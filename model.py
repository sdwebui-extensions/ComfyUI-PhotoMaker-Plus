# Merge image encoder and fuse module to create an ID Encoder
# send multiple ID images, we can directly obtain the updated text encoder containing a stacked ID embedding

import torch
import torch.nn as nn
import comfy.ops
import comfy.clip_model
import comfy.model_management

# code for model from: https://github.com/TencentARC/PhotoMaker/blob/main/photomaker/model.py under Apache License Version 2.0
VISION_CONFIG_DICT = {
    "hidden_size": 1024,
    "image_size": 224,
    "intermediate_size": 4096,
    "num_attention_heads": 16,
    "num_channels": 3,
    "num_hidden_layers": 24,
    "patch_size": 14,
    "projection_dim": 768,
    "hidden_act": "quick_gelu",
    "model_type": "clip_vision_model",
}

class MLP(nn.Module):
    def __init__(self, in_dim, out_dim, hidden_dim, use_residual=True, operations=comfy.ops):
        super().__init__()
        if use_residual:
            assert in_dim == out_dim
        self.layernorm = operations.LayerNorm(in_dim)
        self.fc1 = operations.Linear(in_dim, hidden_dim)
        self.fc2 = operations.Linear(hidden_dim, out_dim)
        self.use_residual = use_residual
        self.act_fn = nn.GELU()

    def forward(self, x):
        residual = x
        x = self.layernorm(x)
        x = self.fc1(x)
        x = self.act_fn(x)
        x = self.fc2(x)
        if self.use_residual:
            x = x + residual
        return x


class FuseModule(nn.Module):
    def __init__(self, embed_dim, operations):
        super().__init__()
        self.mlp1 = MLP(embed_dim * 2, embed_dim, embed_dim, use_residual=False, operations=operations)
        self.mlp2 = MLP(embed_dim, embed_dim, embed_dim, use_residual=True, operations=operations)
        self.layer_norm = operations.LayerNorm(embed_dim)

    def fuse_fn(self, prompt_embeds, id_embeds):
        stacked_id_embeds = torch.cat([prompt_embeds, id_embeds], dim=-1)
        stacked_id_embeds = self.mlp1(stacked_id_embeds) + prompt_embeds
        stacked_id_embeds = self.mlp2(stacked_id_embeds)
        stacked_id_embeds = self.layer_norm(stacked_id_embeds)
        return stacked_id_embeds

    def forward(
        self,
        prompt_embeds,
        id_embeds,
        class_tokens_mask,
    ) -> torch.Tensor:
        # id_embeds shape: [b, max_num_inputs, 1, 2048]
        id_embeds = id_embeds.to(prompt_embeds.dtype)
        num_inputs = class_tokens_mask.sum().unsqueeze(0) # TODO: check for training case
        batch_size, max_num_inputs = id_embeds.shape[:2]
        # seq_length: 77
        seq_length = prompt_embeds.shape[1]
        # flat_id_embeds shape: [b*max_num_inputs, 1, 2048]
        flat_id_embeds = id_embeds.view(
            -1, id_embeds.shape[-2], id_embeds.shape[-1]
        )
        # valid_id_mask [b*max_num_inputs]
        valid_id_mask = (
            torch.arange(max_num_inputs, device=flat_id_embeds.device)[None, :]
            < num_inputs[:, None]
        )
        valid_id_embeds = flat_id_embeds[valid_id_mask.flatten()]

        prompt_embeds = prompt_embeds.view(-1, prompt_embeds.shape[-1])
        class_tokens_mask = class_tokens_mask.view(-1)
        valid_id_embeds = valid_id_embeds.view(-1, valid_id_embeds.shape[-1])
        # slice out the image token embeddings
        image_token_embeds = prompt_embeds[class_tokens_mask]
        stacked_id_embeds = self.fuse_fn(image_token_embeds, valid_id_embeds)
        assert class_tokens_mask.sum() == stacked_id_embeds.shape[0], f"{class_tokens_mask.sum()} != {stacked_id_embeds.shape[0]}"
        prompt_embeds.masked_scatter_(class_tokens_mask[:, None], stacked_id_embeds.to(prompt_embeds.dtype))
        updated_prompt_embeds = prompt_embeds.view(batch_size, seq_length, -1)
        return updated_prompt_embeds

class PhotoMakerIDEncoder(comfy.clip_model.CLIPVisionModelProjection):
    def __init__(self):
        self.load_device = comfy.model_management.text_encoder_device()
        offload_device = comfy.model_management.text_encoder_offload_device()
        dtype = comfy.model_management.text_encoder_dtype(self.load_device)

        super().__init__(VISION_CONFIG_DICT, dtype, offload_device, comfy.ops.manual_cast)
        self.visual_projection_2 = comfy.ops.manual_cast.Linear(1024, 1280, bias=False)
        self.fuse_module = FuseModule(2048, comfy.ops.manual_cast)

    def forward(self, id_pixel_values, prompt_embeds, class_tokens_mask):
        b, num_inputs, c, h, w = id_pixel_values.shape
        id_pixel_values = id_pixel_values.view(b * num_inputs, c, h, w)

        shared_id_embeds = self.vision_model(id_pixel_values)[2]
        id_embeds = self.visual_projection(shared_id_embeds)
        id_embeds_2 = self.visual_projection_2(shared_id_embeds)

        id_embeds = id_embeds.view(b, num_inputs, 1, -1)
        id_embeds_2 = id_embeds_2.view(b, num_inputs, 1, -1)

        id_embeds = torch.cat((id_embeds, id_embeds_2), dim=-1)
        prompt_embeds=prompt_embeds.to(dtype=id_embeds.dtype)
        updated_prompt_embeds = self.fuse_module(prompt_embeds, id_embeds, class_tokens_mask)

        return updated_prompt_embeds

if __name__ == "__main__":
    PhotoMakerIDEncoder()