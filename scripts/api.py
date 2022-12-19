import asyncio
import hashlib
import io
import json
import os
import traceback
import zipfile

import gradio as gr
from fastapi import FastAPI, Response
from pydantic import BaseModel
from pydantic.dataclasses import Union
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

import modules.script_callbacks as script_callbacks
from extensions.sd_dreambooth_extension.dreambooth import dream_state
from extensions.sd_dreambooth_extension.dreambooth.db_config import from_file, DreamboothConfig
from extensions.sd_dreambooth_extension.dreambooth.diff_to_sd import compile_checkpoint
from extensions.sd_dreambooth_extension.dreambooth.sd_to_diff import extract_checkpoint
from extensions.sd_dreambooth_extension.dreambooth.utils import wrap_gpu_call
from extensions.sd_dreambooth_extension.scripts import dreambooth
from extensions.sd_dreambooth_extension.scripts.dreambooth import generate_sample_img
from modules import shared


def zip_files(model_name, files):
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a",
                         zipfile.ZIP_DEFLATED, False) as zip_file:
        for file in files:
            img_byte_arr = io.BytesIO()
            file.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()
            file_name = hashlib.sha1(file.tobytes()).hexdigest()
            image_filename = f"{file_name}.png"
            zip_file.writestr(image_filename, img_byte_arr)
    zip_file.close()
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/x-zip-compressed",
        headers={"Content-Disposition": f"attachment; filename={model_name}_images.zip"}
    )


class DreamboothConcept(BaseModel):
    max_steps: int = -1
    instance_data_dir: str = ""
    class_data_dir: str = ""
    file_prompt_contents: str = ""
    instance_prompt: str = ""
    class_prompt: Union[str, None] = ""
    save_sample_prompt: Union[str, None] = ""
    save_sample_template: Union[str, None] = ""
    instance_token: Union[str, None] = ""
    class_token: Union[str, None] = ""
    num_class_images: int = 0
    class_negative_prompt: Union[str, None] = ""
    class_guidance_scale: float = 7.5
    class_infer_steps: int = 60
    save_sample_negative_prompt: Union[str, None] = ""
    n_save_sample: int = 1
    sample_seed: int = -1
    save_guidance_scale: float = 7.5
    save_infer_steps: int = 60


class DreamboothParameters(BaseModel):
    concepts_list: list[DreamboothConcept]
    adam_beta1: float = 0.9
    adam_beta2: float = 0.999
    adam_epsilon: float = 1e-8
    adam_weight_decay: float = 0.01
    attention: str = "default"
    center_crop: bool = False
    concepts_path: Union[str, None] = ""
    custom_model_name: Union[str, None] = ""
    epoch_pause_frequency: int = 0
    epoch_pause_time: int = 60
    gradient_accumulation_steps: int = 1
    gradient_checkpointing: bool = True
    half_model: bool = False
    hflip: bool = True
    learning_rate: float = 0.000002
    lora_learning_rate: float = 0.0002
    lora_txt_learning_rate: float = 0.0002
    lora_txt_weight: int = 1
    lora_weight: int = 1
    lr_scheduler: str = "constant"
    lr_warmup_steps: int = 500
    max_token_length: int = 75
    max_train_steps: int = 0
    mixed_precision: str = "no"
    model_dir: Union[str, None] = ""
    model_name: str = ""
    not_cache_latents: bool = True
    num_train_epochs: int = 100
    pad_tokens: bool = True
    pretrained_model_name_or_path: Union[str, None] = ""
    pretrained_vae_name_or_path: Union[str, None] = ""
    prior_loss_weight: int = 1
    resolution: int = 512
    revision: int = 0
    sample_batch_size: int = 1
    save_ckpt_after: bool = True
    save_ckpt_cancel: bool = True
    save_ckpt_during: bool = True
    save_class_txt: bool = True
    save_embedding_every: int = 500
    save_lora_after: bool = True
    save_lora_cancel: bool = True
    save_lora_during: bool = True
    save_preview_every: int = 500
    save_state_after: bool = True
    save_state_cancel: bool = False
    save_state_during: bool = False
    save_use_global_counts: bool = True
    save_use_epochs: bool = False
    scale_lr: bool = False
    src: Union[str, None] = ""
    shuffle_tags: bool = False
    train_batch_size: int = 1
    train_text_encoder: bool = True
    use_8bit_adam: bool = False


def dreamBoothAPI(demo: gr.Blocks, app: FastAPI):
    @app.post("/dreambooth/createModel")
    async def createModel(
            db_new_model_name,
            db_new_model_src,
            db_new_model_scheduler,
            db_create_from_hub,
            db_new_model_url,
            db_new_model_token,
            db_new_model_extract_ema):
        print("Creating new Checkpoint: " + db_new_model_name)
        fn = extract_checkpoint(db_new_model_name,
                                db_new_model_src,
                                db_new_model_scheduler,
                                db_create_from_hub,
                                db_new_model_url,
                                db_new_model_token,
                                db_new_model_extract_ema)

    @app.post("/dreambooth/start_training")
    async def start_training(params: DreamboothParameters = None, model_name: str = "", lora_model_name: str = "",
                             lora_weight: int = 1, lora_txt_weight: int = 1, use_imagic: bool = False,
                             use_subdir: bool = False, custom_name: str = "", use_tx2img: bool = False):
        print("Starting Training")
        if params is not None:
            model_name = params.model_name
            lora_weight = params.lora_weight
            lora_txt_weight = params.lora_txt_weight

        task = asyncio.create_task(train_model(model_name, lora_model_name, lora_weight, lora_txt_weight, use_imagic,
                                               use_subdir, custom_name, use_tx2img))
        return {"status": "finished"}

    async def train_model(db_model_name,
                          db_lora_model_name,
                          db_lora_weight,
                          db_lora_txt_weight,
                          db_train_imagic_only,
                          db_use_subdir,
                          db_custom_model_name,
                          db_use_txt2img):

        wrap_gpu_call(dreambooth.start_training(
            db_model_name,
            db_lora_model_name,
            db_lora_weight,
            db_lora_txt_weight,
            db_train_imagic_only,
            db_use_subdir,
            db_custom_model_name,
            db_use_txt2img
        ))

    @app.get("/dreambooth/status")
    async def check_status():
        return {"current_state": f"{json.dumps(dream_state.status.dict())}"}

    @app.get("/dreambooth/model_config")
    async def get_model_config(model_name):
        cfg = from_file(model_name)
        if cfg:
            return JSONResponse(content=cfg.__dict__)
        return {"Exception": "Config not found."}

    @app.post("/dreambooth/model_config")
    async def set_model_config(model_cfg: DreamboothParameters):
        try:
            print("Create config")
            config = DreamboothConfig()
            for key in model_cfg.dict():
                if key in config.__dict__:
                    config.__dict__[key] = model_cfg.dict()[key]
            config.save()
            print("Saved?")
            return JSONResponse(content=config.__dict__)
        except Exception as e:
            traceback.print_exc()
            return {"Exception saving model": f"{e}"}

    @app.get("/dreambooth/get_checkpoint")
    async def get_checkpoint(model_name: str, skip_build=True, lora_model_name: str = "", save_model_name: str = "", lora_weight: int = 1, lora_text_weight: int = 1):
        config = from_file(model_name)
        path = None
        if save_model_name == "" or save_model_name is None:
            save_model_name = model_name
        if skip_build:
            ckpt_dir = shared.cmd_opts.ckpt_dir
            models_path = os.path.join(shared.models_path, "Stable-diffusion")
            if ckpt_dir is not None:
                models_path = ckpt_dir
            use_subdir = False
            if "use_subdir" in config.__dict__:
                use_subdir = config["use_subdir"]
            total_steps = config.revision
            if use_subdir:
                checkpoint_path = os.path.join(models_path, save_model_name, f"{save_model_name}_{total_steps}.ckpt")
            else:
                checkpoint_path = os.path.join(models_path, f"{save_model_name}_{total_steps}.ckpt")
            print(f"Looking for checkpoint at {checkpoint_path}")
            if os.path.exists(checkpoint_path):
                print("Existing checkpoint found, returning.")
                path = checkpoint_path
            else:
                skip_build = False
        if not skip_build:
            ckpt_result = compile_checkpoint(model_name, config.half_model, False, lora_model_name, lora_weight,
                                             lora_text_weight, save_model_name, False, True)
            if "Checkpoint compiled successfully" in ckpt_result:
                path = ckpt_result.replace("Checkpoint compiled successfully:", "").strip()
                print(f"Checkpoint aved to path: {path}")

        if path is not None and os.path.exists(path):
            print(f"Returning file response: {path}-{os.path.splitext(path)}")
            return FileResponse(path)

        return {"exception": f"Unable to find or compile checkpoint."}

    @app.get("/dreambooth/samples")
    async def generate_image(model_name: str, sample_prompt: str, num_images: int = 1, batch_size: int = 1,
                             lora_model_path: str = "", lora_weight: float = 1.0, lora_txt_weight: float = 1.0,
                             negative_prompt: str = "", steps: int = 60, scale: float = 7.5):
        images, msg = generate_sample_img(model_name, sample_prompt,negative_prompt, -1, num_images, steps, scale)
        if len(images) > 1:
            return zip_files(model_name, images)
        else:
            img_byte_arr = io.BytesIO()
            file = images[0]
            file.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()

        return Response(content=img_byte_arr, media_type="image/png")



script_callbacks.on_app_started(dreamBoothAPI)

print("Dreambooth API layer loaded")
