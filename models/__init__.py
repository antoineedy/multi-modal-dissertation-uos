from models.segmentor.zegclip import ZegCLIP

from models.segmentor.xx_scales_input_zegclip import MultiScalesZegCLIP
from models.segmentor.xx_scales_input_zegclip_2 import MultiScalesZegCLIP2
from models.segmentor.xx_scales_output_zegclip import MultiScalesOutputZegCLIP
from models.segmentor.inner_zegclip import InnerZegCLIP
from models.segmentor.dilation_zegclip import DilationZegCLIP
from models.segmentor.inner_bis_zegclip import InnerBisZegCLIP
from models.segmentor.double_inner_zegclip import DoubleInnerZegCLIP

from models.other_modules.multi_scale import MultiScales

from models.backbone.text_encoder import CLIPTextEncoder, DPTCLIPTextEncoder
from models.backbone.img_encoder import (
    CLIPVisionTransformer,
    VPTCLIPVisionTransformer,
    InnerVPTCLIPVisionTransformer,
)

from models.decode_heads.decode_seg import ATMSingleHeadSeg

from models.decode_heads.inner_decode_seg import InnerATMSingleHeadSeg

from models.losses.atm_loss import SegLossPlus

from configs._base_.datasets.dataloader.voc12 import ZeroPascalVOCDataset20
from configs._base_.datasets.dataloader.coco_stuff import ZeroCOCOStuffDataset
