import math
from typing import TYPE_CHECKING, Any, Generic, TypeVar, overload, List, Callable

from jaxtyping import Float
from PIL import Image
from torch import Tensor, cat, device as Device, dtype as DType, nn, softmax, tensor, zeros_like

import refiners.fluxion.layers as fl
from refiners.fluxion.adapters.adapter import Adapter
from refiners.fluxion.context import Contexts
from refiners.fluxion.layers.attentions import ScaledDotProductAttention
from refiners.fluxion.utils import image_to_tensor, normalize
from refiners.foundationals.clip.image_encoder import CLIPImageEncoderH
from refiners.foundationals.dinov2 import ViT

if TYPE_CHECKING:
    from refiners.foundationals.latent_diffusion.stable_diffusion_1.unet import SD1UNet
    from refiners.foundationals.latent_diffusion.stable_diffusion_xl.unet import SDXLUNet

T = TypeVar("T", bound="SD1UNet | SDXLUNet")
TIPAdapter = TypeVar("TIPAdapter", bound="IPAdapter[Any]")  # Self (see PEP 673)


class ImageProjection(fl.Chain):
    def __init__(
        self,
        image_embedding_dim: int = 1024,
        clip_text_embedding_dim: int = 768,
        num_tokens: int = 4,
        use_bias: bool = True,
        device: Device | str | None = None,
        dtype: DType | None = None,
    ) -> None:
        self.image_embedding_dim = image_embedding_dim
        self.clip_text_embedding_dim = clip_text_embedding_dim
        self.num_tokens = num_tokens
        super().__init__(
            fl.Linear(
                in_features=image_embedding_dim,
                out_features=clip_text_embedding_dim * num_tokens,
                bias=use_bias,
                device=device,
                dtype=dtype,
            ),
            fl.Reshape(num_tokens, clip_text_embedding_dim),
            fl.LayerNorm(normalized_shape=clip_text_embedding_dim, bias=use_bias, device=device, dtype=dtype),
        )


class FeedForward(fl.Chain):
    def __init__(
        self,
        embedding_dim: int,
        feedforward_dim: int,
        device: Device | str | None = None,
        dtype: DType | None = None,
    ) -> None:
        self.embedding_dim = embedding_dim
        self.feedforward_dim = feedforward_dim
        super().__init__(
            fl.Linear(
                in_features=self.embedding_dim,
                out_features=self.feedforward_dim,
                bias=False,
                device=device,
                dtype=dtype,
            ),
            fl.GeLU(),
            fl.Linear(
                in_features=self.feedforward_dim,
                out_features=self.embedding_dim,
                bias=False,
                device=device,
                dtype=dtype,
            ),
        )


# Adapted from https://github.com/tencent-ailab/IP-Adapter/blob/6212981/ip_adapter/resampler.py
# See also:
# - https://github.com/mlfoundations/open_flamingo/blob/main/open_flamingo/src/helpers.py
# - https://github.com/lucidrains/flamingo-pytorch
class PerceiverScaledDotProductAttention(fl.Module):
    def __init__(self, head_dim: int, num_heads: int) -> None:
        super().__init__()
        self.num_heads = num_heads
        # See https://github.com/tencent-ailab/IP-Adapter/blob/6212981/ip_adapter/resampler.py#L69
        # -> "More stable with f16 than dividing afterwards"
        self.scale = 1 / math.sqrt(math.sqrt(head_dim))

    def forward(
        self,
        key_value: Float[Tensor, "batch sequence_length 2*head_dim*num_heads"],
        query: Float[Tensor, "batch num_tokens head_dim*num_heads"],
    ) -> Float[Tensor, "batch num_tokens head_dim*num_heads"]:
        bs, length, _ = query.shape
        key, value = key_value.chunk(2, dim=-1)

        q = self.reshape_tensor(query)
        k = self.reshape_tensor(key)
        v = self.reshape_tensor(value)

        attention = (q * self.scale) @ (k * self.scale).transpose(-2, -1)
        attention = softmax(input=attention.float(), dim=-1).type(attention.dtype)
        attention = attention @ v

        return attention.permute(0, 2, 1, 3).reshape(bs, length, -1)

    def reshape_tensor(
        self, x: Float[Tensor, "batch length head_dim*num_heads"]
    ) -> Float[Tensor, "batch num_heads length head_dim"]:
        bs, length, _ = x.shape
        x = x.view(bs, length, self.num_heads, -1)
        x = x.transpose(1, 2)
        x = x.reshape(bs, self.num_heads, length, -1)
        return x


class PerceiverAttention(fl.Chain):
    def __init__(
        self,
        embedding_dim: int,
        head_dim: int = 64,
        num_heads: int = 8,
        device: Device | str | None = None,
        dtype: DType | None = None,
        use_bias: bool = True,
    ) -> None:
        self.embedding_dim = embedding_dim
        self.head_dim = head_dim
        self.inner_dim = head_dim * num_heads
        super().__init__(
            fl.Distribute(
                fl.LayerNorm(normalized_shape=self.embedding_dim, bias=use_bias, device=device, dtype=dtype),
                fl.LayerNorm(normalized_shape=self.embedding_dim, bias=use_bias, device=device, dtype=dtype),
            ),
            fl.Parallel(
                fl.Chain(
                    fl.Lambda(func=self.to_kv),
                    fl.Linear(
                        in_features=self.embedding_dim,
                        out_features=2 * self.inner_dim,
                        bias=False,
                        device=device,
                        dtype=dtype,
                    ),  # Wkv
                ),
                fl.Chain(
                    fl.GetArg(index=1),
                    fl.Linear(
                        in_features=self.embedding_dim,
                        out_features=self.inner_dim,
                        bias=False,
                        device=device,
                        dtype=dtype,
                    ),  # Wq
                ),
            ),
            PerceiverScaledDotProductAttention(head_dim=head_dim, num_heads=num_heads),
            fl.Linear(
                in_features=self.inner_dim, out_features=self.embedding_dim, bias=False, device=device, dtype=dtype
            ),
        )

    def to_kv(self, x: Tensor, latents: Tensor) -> Tensor:
        return cat((x, latents), dim=-2)


class LatentsToken(fl.Chain):
    def __init__(
        self, num_tokens: int, latents_dim: int, device: Device | str | None = None, dtype: DType | None = None
    ) -> None:
        self.num_tokens = num_tokens
        self.latents_dim = latents_dim
        super().__init__(fl.Parameter(num_tokens, latents_dim, device=device, dtype=dtype))


class Transformer(fl.Chain):
    pass


class TransformerLayer(fl.Chain):
    pass


class PerceiverResampler(fl.Chain):
    def __init__(
        self,
        latents_dim: int = 1024,
        num_attention_layers: int = 8,
        num_attention_heads: int = 16,
        head_dim: int = 64,
        num_tokens: int = 8,
        input_dim: int = 768,
        output_dim: int = 1024,
        device: Device | str | None = None,
        dtype: DType | None = None,
        use_bias: bool = True,
    ) -> None:
        self.latents_dim = latents_dim
        self.num_attention_layers = num_attention_layers
        self.head_dim = head_dim
        self.num_attention_heads = num_attention_heads
        self.num_tokens = num_tokens
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.feedforward_dim = 4 * self.latents_dim
        super().__init__(
            fl.Linear(in_features=input_dim, out_features=latents_dim, bias=use_bias, device=device, dtype=dtype),
            fl.SetContext(context="perceiver_resampler", key="x"),
            LatentsToken(num_tokens, latents_dim, device=device, dtype=dtype),
            Transformer(
                TransformerLayer(
                    fl.Residual(
                        fl.Parallel(fl.UseContext(context="perceiver_resampler", key="x"), fl.Identity()),
                        PerceiverAttention(
                            embedding_dim=latents_dim,
                            head_dim=head_dim,
                            num_heads=num_attention_heads,
                            use_bias=use_bias,
                            device=device,
                            dtype=dtype,
                        ),
                    ),
                    fl.Residual(
                        fl.LayerNorm(normalized_shape=latents_dim, bias=use_bias, device=device, dtype=dtype),
                        FeedForward(
                            embedding_dim=latents_dim, feedforward_dim=self.feedforward_dim, device=device, dtype=dtype
                        ),
                    ),
                )
                for _ in range(num_attention_layers)
            ),
            fl.Linear(in_features=latents_dim, out_features=output_dim, bias=use_bias, device=device, dtype=dtype),
            fl.LayerNorm(normalized_shape=output_dim, bias=use_bias, device=device, dtype=dtype),
        )

    def init_context(self) -> Contexts:
        return {"perceiver_resampler": {"x": None}}

def expand_dim(x: Float[Tensor, "batch embed_dim"], sequence_length: int = -1) -> Float[Tensor, "batch seq_len embed_dim"]:
    if sequence_length == -1:
        return x
    return x[:, None].repeat([1, sequence_length, 1])


class ImageCrossAttention(fl.Chain):
    def __init__(
        self,
        text_cross_attention: fl.Attention,
        scale: float = 1.0,
        use_timestep_embedding: bool = False,
        use_pooled_text_embedding: bool = False,
        sequence_length: int = -1,
    ) -> None:
        self._scale = scale
        self.sequence_length = sequence_length
        key_contexts: List[fl.Chain] = [
            fl.Chain(
                fl.UseContext(context="ip_adapter", key="image_embedding"),
                fl.Linear(
                    in_features=text_cross_attention.key_embedding_dim,
                    out_features=text_cross_attention.inner_dim,
                    bias=text_cross_attention.use_bias,
                    device=text_cross_attention.device,
                    dtype=text_cross_attention.dtype,
                ),
            ),
        ]
        query_contexts: List[fl.Chain] = [
            fl.Chain(
                fl.UseContext(context="ip_adapter", key="image_embedding"),
                fl.Linear(
                    in_features=text_cross_attention.value_embedding_dim,
                    out_features=text_cross_attention.inner_dim,
                    bias=text_cross_attention.use_bias,
                    device=text_cross_attention.device,
                    dtype=text_cross_attention.dtype,
                ),
            ),
        ]
        if use_timestep_embedding:
            key_contexts.append(
                fl.Chain(
                    fl.UseContext(context="range_adapter", key="timestep_embedding"),
                    fl.Linear(
                        in_features=1280,
                        out_features=text_cross_attention.inner_dim,
                        bias=text_cross_attention.use_bias,
                        device=text_cross_attention.device,
                        dtype=text_cross_attention.dtype,
                    ),
                    fl.Lambda(lambda x: expand_dim(x, sequence_length=sequence_length))
                )
            )
            query_contexts.append(
                fl.Chain(
                    fl.UseContext(context="range_adapter", key="timestep_embedding"),
                    fl.Linear(
                        in_features=1280,
                        out_features=text_cross_attention.inner_dim,
                        bias=text_cross_attention.use_bias,
                        device=text_cross_attention.device,
                        dtype=text_cross_attention.dtype,
                    ),
                    fl.Lambda(lambda x: expand_dim(x, sequence_length=sequence_length))
                )
            )
        if use_pooled_text_embedding:
            key_contexts.append(
                fl.Chain(
                    fl.UseContext(context="ip_adapter", key="pooled_text_timestep_embedding"),
                    fl.Linear(
                        in_features=1280,
                        out_features=text_cross_attention.inner_dim,
                        bias=text_cross_attention.use_bias,
                        device=text_cross_attention.device,
                        dtype=text_cross_attention.dtype,
                    ),
                    fl.Lambda(lambda x: expand_dim(x, sequence_length=sequence_length))
                )
            )
            query_contexts.append(
                fl.Chain(
                    fl.UseContext(context="ip_adapter", key="pooled_text_timestep_embedding"),
                    fl.Linear(
                        in_features=1280,
                        out_features=text_cross_attention.inner_dim,
                        bias=text_cross_attention.use_bias,
                        device=text_cross_attention.device,
                        dtype=text_cross_attention.dtype,
                    ),
                    fl.Lambda(lambda x: expand_dim(x, sequence_length=sequence_length))
                )
            )

        super().__init__(
            fl.Distribute(
                fl.Identity(),
                fl.Sum(
                    *key_contexts
                ),
                fl.Sum(
                    *query_contexts
                ),
            ),
            ScaledDotProductAttention(
                num_heads=text_cross_attention.num_heads, is_causal=text_cross_attention.is_causal
            ),
            fl.Multiply(self.scale),
        )
    @property
    def scale(self) -> float:
        return self._scale

    @scale.setter
    def scale(self, value: float) -> None:
        self._scale = value
        self.ensure_find(fl.Multiply).scale = value


class CrossAttentionAdapter(fl.Chain, Adapter[fl.Attention]):
    def __init__(
        self,
        target: fl.Attention,
        scale: float = 1.0,
        use_timestep_embedding: bool = False,
        use_pooled_text_embedding: bool = False,
        sequence_length: int = -1,
    ) -> None:
        self._scale = scale
        with self.setup_adapter(target):
            clone = target.structural_copy()
            scaled_dot_product = clone.ensure_find(ScaledDotProductAttention)
            image_cross_attention = ImageCrossAttention(
                text_cross_attention=clone,
                scale=self.scale,
                use_timestep_embedding=use_timestep_embedding,
                use_pooled_text_embedding=use_pooled_text_embedding,
                sequence_length=sequence_length
            )
            clone.replace(
                old_module=scaled_dot_product,
                new_module=fl.Sum(
                    scaled_dot_product,
                    image_cross_attention,
                ),
            )
            super().__init__(
                clone,
            )

    @property
    def image_cross_attention(self) -> ImageCrossAttention:
        return self.ensure_find(ImageCrossAttention)

    @property
    def image_key_projection(self) -> fl.Sum:
        return self.image_cross_attention.layer(("Distribute", 1, "Sum"), fl.Sum)

    @property
    def image_value_projection(self) -> fl.Sum:
        return self.image_cross_attention.layer(("Distribute", 2, "Sum"), fl.Sum)

    @property
    def scale(self) -> float:
        return self._scale

    @scale.setter
    def scale(self, value: float) -> None:
        self._scale = value
        self.image_cross_attention.scale = value

    def load_weights(self, key_tensor: Tensor, value_tensor: Tensor) -> None:
        self.image_key_projection.weight = nn.Parameter(key_tensor)
        self.image_value_projection.weight = nn.Parameter(value_tensor)
        self.image_cross_attention.to(self.device, self.dtype)


class PooledTextEmbeddingTimestepEncoder(fl.Passthrough):
    def __init__(
        self,
        use_bias: bool = True,
        device: Device | str | None = None,
        dtype: DType | None = None,
    ) -> None:
        super().__init__(
            fl.UseContext("ip_adapter", "pooled_text_embedding"),
            fl.Linear(768, 1280, bias=use_bias, device=device, dtype=dtype),
            fl.SetContext("ip_adapter", "pooled_text_timestep_embedding"),
        )


class IPAdapter(Generic[T], fl.Chain, Adapter[T]):
    """Image Prompt adapter for a Stable Diffusion U-Net model.

    See [[arXiv:2308.06721] IP-Adapter: Text Compatible Image Prompt Adapter for Text-to-Image Diffusion Models](https://arxiv.org/abs/2308.06721)
    for more details.
    """

    # Prevent PyTorch module registration
    _image_encoder: list[CLIPImageEncoderH | ViT]
    _grid_image_encoder: list[CLIPImageEncoderH | ViT]
    _image_proj: list[fl.Module]

    def __init__(
        self,
        target: T,
        image_encoder: CLIPImageEncoderH | ViT,
        image_proj: fl.Module,
        scale: float = 1.0,
        fine_grained: bool = False,
        weights: dict[str, Tensor] | None = None,
        strict: bool = True,
        use_timestep_embedding: bool = False,
        use_pooled_text_embedding: bool = False,
        use_bias: bool = True,
        sequence_length: int = -1
    ) -> None:
        """Initialize the adapter.

        Args:
            target: The target model to adapt.
            clip_image_encoder: The CLIP image encoder to use.
            image_proj: The image projection to use.
            scale: The scale to use for the image prompt.
            fine_grained: Whether to use fine-grained image prompt.
            weights: The weights of the IPAdapter.
        """
        with self.setup_adapter(target):
            super().__init__(target)
        self.use_pooled_text_embedding = use_pooled_text_embedding
        if use_pooled_text_embedding:
            self.pooled_text_embedding_proj = PooledTextEmbeddingTimestepEncoder(
                use_bias, self.target.device, self.target.dtype
            )
        self.fine_grained = fine_grained
        if fine_grained:
            self._grid_image_encoder = [self.convert_to_grid_features(image_encoder)]
        else:
            self._image_encoder = [self.convert_to_pooled_features(image_encoder)]

        self._image_proj = [image_proj]

        self.sub_adapters = [
            CrossAttentionAdapter(
                target=cross_attn,
                scale=scale,
                use_timestep_embedding=use_timestep_embedding,
                use_pooled_text_embedding=use_pooled_text_embedding,
                sequence_length=sequence_length
            )
            for cross_attn in filter(lambda attn: type(attn) != fl.SelfAttention, target.layers(fl.Attention))
        ]

        if weights is not None:
            image_proj_state_dict: dict[str, Tensor] = {
                k.removeprefix("image_proj."): v for k, v in weights.items() if k.startswith("image_proj.")
            }
            # Hack to go around image projection with same weight name but different projection shape.
            try:
                self.image_proj.load_state_dict(image_proj_state_dict, strict=strict)
            except Exception as e:
                print(e)
                None

            for i, cross_attn in enumerate(self.sub_adapters):
                cross_attention_weights: dict[str, Tensor] = {}
                for k, v in weights.items():
                    prefix = f"ip_adapter.{i:03d}."
                    if not k.startswith(prefix):
                        continue
                    cross_attention_weights[k[len(prefix):]] = v
                print(len(cross_attention_weights))
                print(cross_attn.state_dict().keys())
                cross_attn.load_state_dict(cross_attention_weights, strict=False)
            if use_pooled_text_embedding:
                pooled_text_embedding_proj_state_dict: dict[str, Tensor] = {
                    k.removeprefix("pooled_text_embedding_proj."): v
                    for k, v in weights.items()
                    if k.startswith("pooled_text_embedding_proj.")
                }
                self.pooled_text_embedding_proj.load_state_dict(pooled_text_embedding_proj_state_dict, strict=strict)

    @property
    def image_encoder(self) -> CLIPImageEncoderH | ViT:
        """The image encoder of the adapter."""
        if not self.fine_grained:
            return self._image_encoder[0]
        else:
            assert hasattr(self, "_grid_image_encoder")
            return self._grid_image_encoder[0]

    @property
    def image_proj(self) -> fl.Module:
        return self._image_proj[0]

    def inject(self: "TIPAdapter", parent: fl.Chain | None = None) -> "TIPAdapter":
        for adapter in self.sub_adapters:
            adapter.inject()
        if self.use_pooled_text_embedding:
            self.target.insert(0, self.pooled_text_embedding_proj)
        return super().inject(parent)

    def eject(self) -> None:
        for adapter in self.sub_adapters:
            adapter.eject()
        if self.use_pooled_text_embedding:
            self.target.pop(0)
        super().eject()

    @property
    def scale(self) -> float:
        """The scale of the adapter."""
        return self.sub_adapters[0].scale

    @scale.setter
    def scale(self, value: float) -> None:
        for cross_attn in self.sub_adapters:
            cross_attn.scale = value

    def set_scale(self, scale: float) -> None:
        for cross_attn in self.sub_adapters:
            cross_attn.scale = scale

    def set_image_embedding(self, image_embedding: Tensor) -> None:
        """Set the image embedding context.

        Note:
            This is required by `ImageCrossAttention`.

        Args:
            image_embedding: The image embedding to set.
        """
        self.set_context("ip_adapter", {"image_embedding": image_embedding})

    def set_pooled_text_embedding(self, pooled_text_embedding: Tensor) -> None:
        self.set_context("ip_adapter", {"pooled_text_embedding": pooled_text_embedding})
    @overload
    def compute_image_embedding(self, image_prompt: Tensor, weights: list[float] | None = None, div_factor: float = 1) -> Tensor:
        ...

    @overload
    def compute_image_embedding(self, image_prompt: Image.Image, div_factor: float = 1) -> Tensor:
        ...

    @overload
    def compute_image_embedding(
        self, image_prompt: list[Image.Image], weights: list[float] | None = None, div_factor: float = 1
    ) -> Tensor:
        ...
    # These should be concatenated to the CLIP text embedding before setting the UNet context
    def compute_image_embedding(self, image_prompt: Tensor | Image.Image | list[Image.Image],
        weights: list[float] | None = None,
        div_factor: float = 1,
        concat_batches: bool = True,
        size: tuple[int, int] = (224, 224),
    ) -> Tensor:
        """Compute the image embedding.

        Args:
            image_prompt: The image prompt to use.
            weights: The scale to use for the image prompt.
            concat_batches: Whether to concatenate the batches.

        Returns:
            The image embedding.
        """
        if isinstance(image_prompt, Image.Image):
            image_prompt = self.preprocess_image(image_prompt, size=size)
        elif isinstance(image_prompt, list):
            assert all(isinstance(image, Image.Image) for image in image_prompt)
            image_prompt = cat([self.preprocess_image(image, size=size) for image in image_prompt])

        negative_embedding, conditional_embedding = self._compute_image_embedding(image_prompt, div_factor)

        batch_size = image_prompt.shape[0]
        if weights is not None:
            assert len(weights) == batch_size, f"Got {len(weights)} weights for {batch_size} images"
            if any(weight != 1.0 for weight in weights):
                conditional_embedding *= (
                    tensor(weights, device=conditional_embedding.device, dtype=conditional_embedding.dtype)
                    .unsqueeze(-1)
                    .unsqueeze(-1)
                )

        if batch_size > 1 and concat_batches:
            # Create a longer image tokens sequence when a batch of images is given
            # See https://github.com/tencent-ailab/IP-Adapter/issues/99
            negative_embedding = cat(negative_embedding.chunk(batch_size), dim=1)
            conditional_embedding = cat(conditional_embedding.chunk(batch_size), dim=1)

        return cat((negative_embedding, conditional_embedding))


    def _compute_image_embedding(self, image_prompt: Tensor, div_factor: float = 1) -> tuple[Tensor, Tensor]:
        image_encoder = self.image_encoder
        image_embedding = image_encoder(image_prompt)
        image_embedding /= div_factor
        conditional_embedding = self.image_proj(image_embedding)
        if not self.fine_grained:
            negative_embedding = self.image_proj(zeros_like(image_embedding))
        else:
            # See https://github.com/tencent-ailab/IP-Adapter/blob/d580c50/tutorial_train_plus.py#L351-L352
            image_embedding = image_encoder(zeros_like(image_prompt))
            image_embedding /= div_factor
            negative_embedding = self.image_proj(image_embedding)
        return negative_embedding, conditional_embedding

    def preprocess_image(
        self,
        image: Image.Image,
        size: tuple[int, int] = (224, 224),
        mean: list[float] | None = None,
        std: list[float] | None = None,
    ) -> Tensor:
        """Preprocess the image.

        Note:
            The default mean and std are parameters from
            https://github.com/openai/CLIP

        Args:
            image: The image to preprocess.
            size: The size to resize the image to.
            mean: The mean to use for normalization.
            std: The standard deviation to use for normalization.
        """
        return normalize(
            image_to_tensor(image.resize(size), device=self.target.device, dtype=self.target.dtype),
            mean=[0.48145466, 0.4578275, 0.40821073] if mean is None else mean,
            std=[0.26862954, 0.26130258, 0.27577711] if std is None else std,
        )

    @staticmethod
    def convert_to_pooled_features(image_encoder: CLIPImageEncoderH | ViT) -> CLIPImageEncoderH | ViT:
        encoder_clone = image_encoder.structural_copy()
        if isinstance(image_encoder, CLIPImageEncoderH):
            return encoder_clone
        else:
            assert isinstance(encoder_clone[-1], fl.LayerNorm)  # final normalization
            pooling_func: Callable[Tensor, Tensor] = lambda x: x[:, 0]
            encoder_clone.append(fl.Lambda(pooling_func))
        return encoder_clone

    @staticmethod
    def convert_to_grid_features(image_encoder: CLIPImageEncoderH | ViT) -> CLIPImageEncoderH | ViT:
        encoder_clone = image_encoder.structural_copy()
        if isinstance(image_encoder, CLIPImageEncoderH):
            assert isinstance(encoder_clone[-1], fl.Linear)  # final proj
            assert isinstance(encoder_clone[-2], fl.LayerNorm)  # final normalization
            assert isinstance(encoder_clone[-3], fl.Lambda)  # pooling (classif token)
            for _ in range(3):
                encoder_clone.pop()
            transformer_layers = encoder_clone[-1]
            assert isinstance(transformer_layers, fl.Chain) and len(transformer_layers) == 32
            transformer_layers.pop()
        else:
            assert isinstance(encoder_clone[-1], fl.LayerNorm)  # final normalization
            encoder_clone.pop()
        return encoder_clone
