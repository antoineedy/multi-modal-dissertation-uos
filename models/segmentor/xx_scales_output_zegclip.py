from matplotlib.pyplot import text
import torch
import torch.nn as nn
import torch.nn.functional as F

from mmseg.core import add_prefix  # type: ignore
from mmseg.ops import resize  # type: ignore
from mmseg.models import builder
from mmseg.models.builder import SEGMENTORS
from mmseg.models.segmentors.base import BaseSegmentor
from mmseg.models.segmentors.encoder_decoder import EncoderDecoder

# import sys
# sys.path.append('../')
# from other_modules.multi_scale import MultiScales

from .untils import tokenize
import numpy as np
import tqdm

from copy import deepcopy

# from PIL import Image

import os
import matplotlib.pyplot as plt

def patchify(x: torch.Tensor, dimension=2):
    """
    Patchify a tensor.
    
    :param x: tensor to patchify
    :param dimension: number of cuts to do along each dimension. 
                      2 means creating a grid of 2*2 patches (top left, top right, bottom left, bottom right)
    :return: tensor of patches
    """
    x = x.unsqueeze(0)
    assert len(x.shape) >= 2, "Input tensor must have at least 2 dimensions"
    assert all([s % dimension == 0 for s in x.shape[-2:]]), "Tensor dimensions must be divisible by the number of patches"

    patches = x.unfold(2, x.shape[-2] // dimension, x.shape[-2] // dimension) \
              .unfold(3, x.shape[-1] // dimension, x.shape[-1] // dimension)
    patches = patches.contiguous().view(*x.shape[:-2], -1, x.shape[-2] // dimension, x.shape[-1] // dimension)
    patches = patches.squeeze(0).permute(1, 0, 2, 3)
    
    return patches

def depatchify(patches: torch.Tensor, dimension=2):
    """
    Depatchify a tensor.
    
    :param patches: tensor of patches to reconstruct
    :param dimension: number of patches along each dimension.
                      Should match the dimension used in patchify.
    :return: original tensor reconstructed from patches
    """
    patches = patches.permute(0, 2, 1, 3, 4)
    patch_size = patches.shape[-2:]
    num_patches = dimension

    original_shape = list(patches.shape[:-3]) + [patch_size[0] * num_patches, patch_size[1] * num_patches]
    
    # Reshape patches to combine the patch dimensions
    patches = patches.view(*patches.shape[:-3], num_patches, num_patches, *patch_size)
    
    # Permute to bring patch dimensions in order
    patches = patches.permute(0, 1, 2, 4, 3, 5).contiguous()
    
    # Merge patches into the original tensor
    original_tensor = patches.view(*original_shape)
    original_tensor = original_tensor.squeeze(0)
    
    return original_tensor


class MultiScales(nn.Module):
    def __init__(self, divisions):
        super().__init__()
        self.divisions = divisions
        self.original_size = None
        self.upsample = None

    def forward(self, x):
        self.device = x.device
        self.original_size = x.size()[-1]
        self.original_type = x.dtype
        self.upsample = nn.Upsample(
            size=(self.original_size, self.original_size),
            mode="bilinear",
            align_corners=False,
        )
        out = []
        for i in range(len(self.divisions)):
            out += self._create_images(x, self.divisions[i], i)
        out = torch.stack(out).to(self.device)
        return out

    def _create_images(self, x, number_divisions, add_x=0):
        """
        Create images from the original image.

        Args:
            x (torch.Tensor): The original image.
            number_divisions (int): The number of divisions to make in each direction.
                If 2, the image will be divided in 2 parts in each direction, resulting in 4 images.
                The number of images will be number_divisions**2.
        """
        patch_size = int(self.original_size) // int(number_divisions)
        patches = patchify(
            x, 2)
        new_patches = []
        if add_x == 0:
            new_patches.append(x)
        for i in range(len(patches)):
            patch_image = patches[i]  # channel x height x width
            patch_image = patch_image.unsqueeze(0).float()
            patch_image = self.upsample(patch_image)
            patch_image = patch_image.squeeze(0)
            new_patches.append(patch_image)
        patches = torch.stack(new_patches)
        return patches

@SEGMENTORS.register_module()
class MultiScalesOutputZegCLIP(EncoderDecoder):
    """Encoder Decoder segmentors.
    EncoderDecoder typically consists of backbone, decode_head, auxiliary_head.
    Note that auxiliary_head is only used for deep supervision during training,
    which could be dumped during inference.
    """

    def __init__(
        self,
        text_encoder,
        pretrained_text,
        class_names,
        context_length,
        base_class,
        novel_class,
        both_class,
        tau=0.07,
        multi_prompts=False,
        self_training=False,
        ft_backbone=False,
        exclude_key=None,
        load_text_embedding=None,
        #  init_cfg=None,
        **args,
    ):
        super(MultiScalesOutputZegCLIP, self).__init__(**args)

        if pretrained_text is not None:
            assert (
                text_encoder.get("pretrained") is None
            ), "both text encoder and segmentor set pretrained weight"
            text_encoder.pretrained = pretrained_text

        self.text_encoder = builder.build_backbone(text_encoder)

        self.tau = tau
        self.class_names = class_names

        self.base_class = np.asarray(base_class)  # antoine: seen classes
        self.novel_class = np.asarray(novel_class)  # antoine: unseen classes
        self.both_class = np.asarray(both_class)  # antoine: seen + unseen classes
        self.self_training = self_training
        """
        antoine:
        Self-training in Zero-shot Segmentation introduces
        another setting of zero-shot semantic segmentation called
        “transductive”. Unlike the traditional “inductive” setting
        where the novel class names and annotations are both unavailable 
        in the training stage, [34] proposed that selftraining via pseudo
        labels on unlabeled pixels benefits solving the imbalance problem.
        In such “transductive” situation, both the ground truth of seen classes, as well as pseudo
        labels of unseen classes, will be utilized to supervise the
        target model for self-training [17,49,56] which can achieve
        better performance.
        """
        self.multi_prompts = (
            multi_prompts  # antoine: if we have multiple text prompts, see further
        )

        self.load_text_embedding = load_text_embedding  # antoine: if we want to load text embeddings from a file

        self.upsample2 = nn.Upsample(
            scale_factor=2,
            mode="bilinear",
            align_corners=False,
        )

        self.upsample4 = nn.Upsample(
            scale_factor=4,
            mode="bilinear",
            align_corners=False,
        )

        self.upsample8 = nn.Upsample(
            scale_factor=8,
            mode="bilinear",
            align_corners=False,
        )

        # torch.Size([1, 512, 32, 32]) -> torch.Size([1, 512, 16, 16])
        self.conv_mult_1 = nn.Conv2d(512, 512, 3, stride = 2, padding=1)
        

        # torch.Size([1, 512, 16, 16]) -> torch.Size([1, 512, 8, 8])
        self.conv_mult_2 = nn.Conv2d(512, 512, 3, stride = 2, padding=1)

        # torch.Size([1, 512, 8, 8]) -> torch.Size([1, 512, 4, 4])
        self.conv_mult_3 = nn.Conv2d(512, 512, 3, stride = 2, padding=1)

        if not self.load_text_embedding:
            if not self.multi_prompts:
                self.texts = torch.cat(
                    [tokenize(f"a photo of a {c}") for c in self.class_names]
                )
            else:
                self.texts = self._get_multi_prompts(self.class_names)
                # antoine: this one? https://github.com/ltttpku/ADA-CM/blob/8d03033052501b8f6e8fb125aa56a83a028317ed/upt_tip_cache_model_free_finetune_distill3.py#L1406

        if len(self.base_class) != len(self.both_class):  # zero-shot setting
            # antoine: base_class is the seen classes, both_class is the seen + unseen classes.
            # If they are not the same, it means we are in a zero-shot setting
            if (
                not self_training
            ):  # antoine: if we are in training mode, we need to create a mask to make the unseen classes invisible
                self._visiable_mask(self.base_class, self.novel_class, self.both_class)
            else:
                self._visiable_mask_st(
                    self.base_class, self.novel_class, self.both_class
                )
                self._st_mask(self.base_class, self.novel_class, self.both_class)

        if self.training:
            self._freeze_stages(self.text_encoder)
            if ft_backbone is False:
                self._freeze_stages(self.backbone, exclude_key=exclude_key)

        else:
            self.text_encoder.eval()
            self.backbone.eval()

    def _freeze_stages(
        self, model, exclude_key=None
    ):  # antoine: to freeze some layers of the model
        """Freeze stages param and norm stats."""
        for n, m in model.named_parameters():
            if exclude_key:
                if isinstance(exclude_key, str):
                    if not exclude_key in n:
                        m.requires_grad = False
                elif isinstance(exclude_key, list):
                    count = 0
                    for i in range(len(exclude_key)):
                        i_layer = str(exclude_key[i])
                        if i_layer in n:
                            count += 1
                    if count == 0:
                        m.requires_grad = False
                    elif count > 0:
                        print("Finetune layer in backbone:", n)
                else:
                    assert AttributeError("Dont support the type of exclude_key!")
            else:
                m.requires_grad = False

    def _visiable_mask(
        self, seen_classes, novel_classes, both_classes
    ):  # antoine: to create a mask for the zero-shot setting
        seen_map = np.array([-1] * 256)
        # antoine: I think 256 is the number of classes. We create a mask of -1 for all classes. We will then replace the seen classes with their index.
        seen_map[255] = 255
        for i, n in enumerate(list(seen_classes)):
            seen_map[n] = (
                i  # antoine: we replace the seen classes with their index. The unseen classes will remain -1.
            )
        self.visibility_seen_mask = seen_map.copy()
        print("Making visible mask for zero-shot setting:", self.visibility_seen_mask)

    def _visiable_mask_st(self, seen_classes, novel_classes, both_classes):
        seen_map = np.array([-1] * 256)
        seen_map[255] = 255
        for i, n in enumerate(list(seen_classes)):
            seen_map[n] = (
                n  # antoine: we replace the seen classes with their index. Because we are in self-training mode, we keep the unseen classes!!
            )
        seen_map[200] = 200  # pixels of padding will be excluded
        self.visibility_seen_mask = seen_map.copy()
        print(
            "Making visible mask for zero-shot setting in self_traning stage:",
            self.visibility_seen_mask,
        )

    def _st_mask(self, seen_classes, novel_classes, both_classes):
        st_mask = np.array([255] * 256)
        st_mask[255] = 255
        for i, n in enumerate(list(novel_classes)):
            st_mask[n] = n
        self.st_mask = st_mask.copy()
        print(
            "Making st mask for zero-shot setting in self_traning stage:", self.st_mask
        )

    def _init_decode_head(self, decode_head):
        """Initialize ``decode_head``"""
        self.decode_head = builder.build_head(decode_head)
        self.align_corners = self.decode_head.align_corners
        self.num_classes = self.decode_head.num_classes

    def _decode_head_forward_train(self, feat, img_metas, gt_semantic_seg):
        # antoine: feat contains the image features and the text features
        # img_metas contains ?
        # gt_semantic_seg contains the ground truth segmentation

        """Run forward function and calculate loss for decode head in
        training."""
        if self.training:
            if len(self.base_class) != len(self.both_class):  # zero-shot setting
                gt_semantic_seg = torch.Tensor(self.visibility_seen_mask).type_as(
                    gt_semantic_seg
                )[gt_semantic_seg]

        losses = dict()
        if self.self_training:
            loss_decode = self.decode_head.forward_train(
                feat,
                img_metas,
                gt_semantic_seg,
                self.train_cfg,
                self.self_training,
                self.st_mask,
            )
        else:
            loss_decode = self.decode_head.forward_train(
                feat, img_metas, gt_semantic_seg, self.train_cfg, self.self_training
            )

        losses.update(add_prefix(loss_decode, "decode"))
        return losses

    def text_embedding(self, texts, img):
        # antoine: encode the text using the CLIP text encoder
        text_embeddings = self.text_encoder(texts.to(img.device))
        # antoine: we normalize the embeddings
        text_embeddings = text_embeddings / text_embeddings.norm(dim=-1, keepdim=True)
        return text_embeddings

    def extract_feat(self, img):
        # antoine: extract features from images using the backbone. Here, the backbone is the image encoder, CLIP vision encoder
        """Extract features from images."""
        visual_feat = self.backbone(img)
        return visual_feat

    def apply_multiscale_output(self, visual_feat):
        # antoine: apply the multi-scale output
        # visual_feat[0][0]: visual features
        # visual_feat[1][0]: cls token
        backup_visual_feat = list(visual_feat)
        visual_feat_00 = visual_feat[0][0] # torch.Size([1, 512, 32, 32])

        first_step = self.conv_mult_1(visual_feat_00) # torch.Size([1, 512, 16, 16])
        second_step = self.conv_mult_2(first_step) # torch.Size([1, 512, 8, 8])
        #third_step = self.conv_mult_3(second_step) # torch.Size([1, 512, 4, 4])

        a = self.upsample2(first_step)
        b = self.upsample4(second_step)
        #c = self.upsample8(third_step)
        d = visual_feat_00 

        #visual_feat_last = a + b + c + d
        visual_feat_last = a + b + d
        #visual_feat_last = visual_feat_last / 4
        visual_feat_last = visual_feat_last / 3

        backup_visual_feat[0] = list(backup_visual_feat[0])
        backup_visual_feat[0][0] = visual_feat_last

        return backup_visual_feat

    def _upsample(self, x, scale_factor=2):
        upsample = nn.Upsample(
            scale_factor=scale_factor,
            mode="bilinear",
            align_corners=False,
        )
        x = upsample(x)
        return x

    def forward_train(self, img, img_metas, gt_semantic_seg):
        visual_feat = self.extract_feat(
            img
        )  # image features using the CLIP image encoder
        visual_feat = self.apply_multiscale_output(visual_feat)
        if self.load_text_embedding:
            # antoine: if we want to load text embeddings from a file
            text_feat = np.load(self.load_text_embedding)
            text_feat = torch.from_numpy(text_feat).to(img.device)
        else:
            if not self.multi_prompts:
                # antoine: one prompt: "a photo of a {class_name}". We tokenize it and encode it using the CLIP text encoder
                text_feat = self.text_embedding(self.texts, img)
            else:
                # antoine: multiple prompts here. What is that?
                assert AttributeError("preparing the multi embeddings")

        if not self.self_training:
            # antoine: if we are not in self-training mode, we only keep the embeddings of the seen classes
            text_feat = text_feat[self.base_class, :]

        feat = []
        feat.append(visual_feat)
        feat.append(text_feat)

        # antoine: feat contains the image features and the text features

        losses = dict()
        loss_decode = self._decode_head_forward_train(feat, img_metas, gt_semantic_seg)
        losses.update(loss_decode)

        return losses

    def encode_decode(self, img, img_metas):
        visual_feat = self.extract_feat(img)  # antoine: image encoder from CLIP
        visual_feat = self.apply_multiscale_output(visual_feat)
        if self.load_text_embedding:
            text_feat = np.load(self.load_text_embedding)
            text_feat = torch.from_numpy(text_feat).to(img.device)
            # antoine: encode the text using the CLIP text encoder
        else:
            if not self.multi_prompts:
                text_feat = self.text_embedding(self.texts, img)
                # antoine: if one prompt, then simple text encoder from CLIP with the prompt "a photo of a {class_name}"
            else:
                num_cls, num_prompts, _ = self.texts.size()  # the numerous prompts!
                text_feat = self.text_embedding(
                    self.texts.reshape(num_cls * num_prompts, -1), img
                )
                # antoine: if multiple prompts, we reshape the text embeddings and encode them using the CLIP text encoder
                text_feat = text_feat.reshape(num_cls, num_prompts, -1).mean(dim=1)
                # antoine: we take the mean of the embeddings of the prompts
                text_feat /= text_feat.norm(dim=-1).unsqueeze(1)
                # antoine: we normalize the embeddings

        feat = []
        feat.append(visual_feat)
        feat.append(text_feat)
        # antoine: feat contains the image features and the text features

        out = self._decode_head_forward_test(feat, img_metas, self.self_training)
        out = resize(
            input=out,
            size=img.shape[2:],
            mode="bilinear",
            align_corners=self.align_corners,
        )
        return out

    def _decode_head_forward_test(self, x, img_metas, self_training):
        """Run forward function and calculate loss for decode head in
        inference."""
        seg_logits = self.decode_head.forward_test(
            x, img_metas, self.test_cfg, self_training
        )
        return seg_logits

    # TODO refactor
    def slide_inference(self, img, img_meta, rescale):
        """Inference by sliding-window with overlap.

        If h_crop > h_img or w_crop > w_img, the small patch will be used to
        decode without padding.
        """
        h_stride, w_stride = self.test_cfg.stride
        h_crop, w_crop = self.test_cfg.crop_size
        batch_size, _, h_img, w_img = img.size()
        num_classes = len(self.both_class)
        h_grids = max(h_img - h_crop + h_stride - 1, 0) // h_stride + 1
        w_grids = max(w_img - w_crop + w_stride - 1, 0) // w_stride + 1
        preds = img.new_zeros((batch_size, num_classes, h_img, w_img))
        count_mat = img.new_zeros((batch_size, 1, h_img, w_img))
        for h_idx in range(h_grids):
            for w_idx in range(w_grids):
                y1 = h_idx * h_stride
                x1 = w_idx * w_stride
                y2 = min(y1 + h_crop, h_img)
                x2 = min(x1 + w_crop, w_img)
                y1 = max(y2 - h_crop, 0)
                x1 = max(x2 - w_crop, 0)
                crop_img = img[:, :, y1:y2, x1:x2]
                crop_seg_logit = self.encode_decode(crop_img, img_meta)
                preds += F.pad(
                    crop_seg_logit,
                    (
                        int(x1),
                        int(preds.shape[3] - x2),
                        int(y1),
                        int(preds.shape[2] - y2),
                    ),
                )

                count_mat[:, :, y1:y2, x1:x2] += 1
        assert (count_mat == 0).sum() == 0
        if torch.onnx.is_in_onnx_export():
            count_mat = torch.from_numpy(count_mat.cpu().detach().numpy()).to(
                device=img.device
            )
        preds = preds / count_mat
        if rescale:
            preds = resize(
                preds,
                size=img_meta[0]["ori_shape"][:2],
                mode="bilinear",
                align_corners=self.align_corners,
                warning=False,
            )
        return preds