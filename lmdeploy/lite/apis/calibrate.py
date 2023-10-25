# Copyright (c) OpenMMLab. All rights reserved.

from pathlib import Path

import torch
from accelerate import (infer_auto_device_map, init_empty_weights,
                        load_checkpoint_in_model)
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

from lmdeploy.lite.quantization import CalibrationContext
from lmdeploy.lite.utils import collect_target_modules, get_calib_loaders

LAYER_TYPE_MAP = {
    'InternLMForCausalLM': 'InternLMDecoderLayer',
    'QWenLMHeadModel': 'QWenBlock',
    'BaiChuanForCausalLM': 'DecoderLayer',  # Baichuan 7B
    'BaichuanForCausalLM': 'DecoderLayer',  # Baichuan2 7B
    'LlamaForCausalLM': 'LlamaDecoderLayer',
}
NORM_TYPE_MAP = {
    'InternLMForCausalLM': 'InternLMRMSNorm',
    'QWenLMHeadModel': 'RMSNorm',
    'BaiChuanForCausalLM': 'RMSNorm',  # Baichuan 7B
    'BaichuanForCausalLM': 'RMSNorm',  # Baichuan2 7B
    'LlamaForCausalLM': 'LlamaRMSNorm',
}


def _prepare_for_calibrate(model,
                           layer_type,
                           head_name='lm_head',
                           device='cuda',
                           prefix=''):

    for name, child in model.named_children():

        if isinstance(layer_type, str):
            is_layer = type(child).__name__ == layer_type
        elif isinstance(layer_type, type):
            is_layer = isinstance(child, layer_type)
        else:
            raise TypeError

        contain_layer = len(
            collect_target_modules(child, layer_type, [head_name]).keys()) > 0
        is_head = name == head_name

        mod_name = f'{prefix}.{name}' if prefix else name

        if is_layer or is_head:
            child.to('cpu')
            print(f'Move {mod_name} to CPU.')
            continue

        if contain_layer:
            _prepare_for_calibrate(child, layer_type, head_name, device,
                                   mod_name)
        else:
            child.to(device)
            print(f'Move {mod_name} to GPU.')


def calibrate(model: str,
              calib_dataset: str = 'c4',
              calib_samples: int = 128,
              calib_seqlen: int = 2048,
              work_dir: str = './work_dir',
              device: str = 'cuda') -> None:
    """The main function for loading the model and performing calibration on a
    given dataset.

    Args:
        model (str): The model to be loaded.
        calib_dataset (str, optional): The calibration dataset name.
            Defaults to 'c4'.
        calib_samples (int, optional): The number of samples for calibration.
            Defaults to 128.
        calib_seqlen (int, optional): The sequence length for calibration.
            Defaults to 2048.
        work_dir (str): The working directory for outputs.
            Defaults to './work_dir'.
        device (str, optional): The device to be used for calculation.
            Defaults to 'cuda'.
    """

    assert calib_dataset in ['c4', 'ptb', 'wikitext2', 'pileval'], \
        'Support only `c4`, `ptb`, `wikitext2` or `pileval`.'

    # Load tokenizer and configuration
    tokenizer = AutoTokenizer.from_pretrained(model,
                                              use_fast=False,
                                              trust_remote_code=True)
    hf_config = AutoConfig.from_pretrained(model,
                                           torch_dtype=torch.float16,
                                           trust_remote_code=True)
    checkpoint = hf_config._name_or_path

    # hard code for qwen, other configs do not have the `fp16` attribute.
    hf_config.fp16 = True

    with init_empty_weights():
        # Load model
        model = AutoModelForCausalLM.from_pretrained(model,
                                                     config=hf_config,
                                                     torch_dtype=torch.float16,
                                                     trust_remote_code=True)
        model.config.use_cache = False

    layer_type = LAYER_TYPE_MAP[type(model).__name__]
    norm_type = NORM_TYPE_MAP[type(model).__name__]

    decoder_layers = collect_target_modules(model, layer_type)

    # Infer device map
    device_map = infer_auto_device_map(model,
                                       no_split_module_classes=[layer_type])
    for name in device_map.keys():
        if name in decoder_layers or 'lm_head' in name:
            device_map[name] = 'cpu'
        else:
            device_map[name] = 0
    load_checkpoint_in_model(model,
                             checkpoint,
                             device_map,
                             dtype=torch.float16)

    _prepare_for_calibrate(model, layer_type, 'lm_head', device)

    print('Loading calibrate dataset ...')
    calib_loader, _ = get_calib_loaders(calib_dataset,
                                        tokenizer,
                                        nsamples=calib_samples,
                                        seqlen=calib_seqlen)

    # Initialize calibration context
    calib_ctx = CalibrationContext(model,
                                   tokenizer,
                                   layer_type=layer_type,
                                   norm_type=norm_type,
                                   device=device)

    with calib_ctx:
        all_data = torch.cat([
            data if isinstance(data, torch.Tensor) else data[0]
            for data in calib_loader
        ]).to(device)
        calib_ctx.calibrate(all_data)

    # Create work directory if not exists
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    calib_ctx.export(work_dir)


if __name__ == '__main__':
    import fire

    fire.Fire(calibrate)
