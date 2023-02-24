# coding=utf-8
# Copyright 2022 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gc
import tempfile
import time
import unittest

import numpy as np
import PIL.Image
import torch
from transformers import CLIPTextConfig, CLIPTextModel, CLIPTokenizer

from diffusers import (
    AutoencoderKL,
    ControlNetModel,
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    LMSDiscreteScheduler,
    StableDiffusionControlNetPipeline,
    UNet2DConditionModel,
)
from diffusers.utils import randn_tensor, slow, torch_device
from diffusers.utils.import_utils import is_xformers_available
from diffusers.utils.testing_utils import require_torch_gpu

from ...test_pipelines_common import PipelineTesterMixin


torch.backends.cuda.matmul.allow_tf32 = False


class StableDiffusionControlNetPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = StableDiffusionControlNetPipeline

    def get_dummy_components(self):
        torch.manual_seed(0)
        unet = UNet2DConditionModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=4,
            out_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            up_block_types=("CrossAttnUpBlock2D", "UpBlock2D"),
            cross_attention_dim=32,
        )
        torch.manual_seed(0)
        controlnet = ControlNetModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=4,
            down_block_types=("DownBlock2D", "CrossAttnDownBlock2D"),
            cross_attention_dim=32,
            controlnet_conditioning_channels=3,
        )
        torch.manual_seed(0)
        scheduler = DDIMScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            clip_sample=False,
            set_alpha_to_one=False,
        )
        torch.manual_seed(0)
        vae = AutoencoderKL(
            block_out_channels=[32, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
        )
        torch.manual_seed(0)
        text_encoder_config = CLIPTextConfig(
            bos_token_id=0,
            eos_token_id=2,
            hidden_size=32,
            intermediate_size=37,
            layer_norm_eps=1e-05,
            num_attention_heads=4,
            num_hidden_layers=5,
            pad_token_id=1,
            vocab_size=1000,
        )
        text_encoder = CLIPTextModel(text_encoder_config)
        tokenizer = CLIPTokenizer.from_pretrained("hf-internal-testing/tiny-random-clip")

        components = {
            "unet": unet,
            "controlnet": controlnet,
            "scheduler": scheduler,
            "vae": vae,
            "text_encoder": text_encoder,
            "tokenizer": tokenizer,
            "safety_checker": None,
            "feature_extractor": None,
        }
        return components

    def get_dummy_inputs(self, device, seed=0):
        if str(device).startswith("mps"):
            generator = torch.manual_seed(seed)
        else:
            generator = torch.Generator(device=device).manual_seed(seed)

        controlnet_embedder_scale_factor = 8
        image = randn_tensor(
            (1, 3, 32 * controlnet_embedder_scale_factor, 32 * controlnet_embedder_scale_factor),
            generator=generator,
            device=torch.device(device),
        )

        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "generator": generator,
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "numpy",
            "image": image,
        }

        return inputs

    def get_dummy_components_for_controlnet(self):
        components = self.get_dummy_components()
        # vae_scale_factor 8 version
        # this for ControlNetInputHintBlock accepts only vae_scale_factor=8
        components["vae"] = AutoencoderKL(
            block_out_channels=[32, 64, 64, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D", "DownEncoderBlock2D", "DownEncoderBlock2D"],
            up_block_types=["UpDecoderBlock2D", "UpDecoderBlock2D", "UpDecoderBlock2D", "UpDecoderBlock2D"],
            latent_channels=4,
        )
        return components

    def get_dummy_inputs_for_controlnet(self, device, seed=0, num_of_prompts=1, num_images_per_prompt=1):
        inputs = self.get_dummy_inputs(device, seed)
        vae_scale_factor = 8
        if num_of_prompts > 1:
            inputs["prompt"] = [f"a photo of {i} cats" for i in range(num_of_prompts)]

        controlnet_hint = torch.randn(
            (num_of_prompts * num_images_per_prompt, 3, 32 * vae_scale_factor, 32 * vae_scale_factor),
            generator=inputs["generator"],
        )

        controlnet_hint = controlnet_hint.detach().numpy().copy()
        images = np.zeros_like(controlnet_hint, dtype=np.uint8)
        images[controlnet_hint > 0.5] = 255
        images = images.transpose(0, 3, 2, 1)  # b c h w -> b w h c
        if images.shape[0] == 1:
            controlnet_hint = PIL.Image.fromarray(images[0])  # PIL.Image
        else:
            controlnet_hint = [PIL.Image.fromarray(images[b]) for b in range(images.shape[0])]  # List of PIL.Image

        inputs["image"] = controlnet_hint
        inputs["num_images_per_prompt"] = num_images_per_prompt
        return inputs

    def test_attention_slicing_forward_pass(self):
        return self._test_attention_slicing_forward_pass(expected_max_diff=2e-3)

    @unittest.skipIf(
        torch_device != "cuda" or not is_xformers_available(),
        reason="XFormers attention is only available with CUDA and `xformers` installed",
    )
    def test_xformers_attention_forwardGenerator_pass(self):
        self._test_xformers_attention_forwardGenerator_pass(expected_max_diff=2e-3)

    def test_inference_batch_single_identical(self):
        self._test_inference_batch_single_identical(expected_max_diff=2e-3)

    def test_stable_diffusion_controlnet_ddim(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        vae_scale_factor = 8
        components = self.get_dummy_components_for_controlnet()
        sd_pipe = StableDiffusionControlNetPipeline(**components)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs_for_controlnet(device)
        output = sd_pipe(**inputs)
        image = output.images

        image_slice = image[0, -3:, -3:, -1]
        # print("image_slice", image_slice)

        assert image.shape == (1, 32 * vae_scale_factor, 32 * vae_scale_factor, 3)
        expected_slice = np.array(
            [0.47653976, 0.4843403, 0.46522307, 0.39793792, 0.454136, 0.4749748, 0.37724984, 0.4025603, 0.47651842]
        )

        assert np.abs(image_slice.flatten() - expected_slice).max() < 1e-2

    def test_stable_diffusion_controlnet_ddim_two_prompts(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        vae_scale_factor = 8
        components = self.get_dummy_components_for_controlnet()
        sd_pipe = StableDiffusionControlNetPipeline(**components)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs_for_controlnet(device, num_of_prompts=2)
        output = sd_pipe(**inputs)
        image = output.images

        image_slice0 = image[0, -3:, -3:, -1]
        image_slice1 = image[1, -3:, -3:, -1]

        # print("image_slice0", image_slice0)
        # print("image_slice1", image_slice1)

        assert image.shape == (2, 32 * vae_scale_factor, 32 * vae_scale_factor, 3)

        expected_slice0 = np.array(
            [0.4394728, 0.46073985, 0.49796283, 0.52271855, 0.51414967, 0.5314792, 0.47262335, 0.47206822, 0.48990324]
        )
        expected_slice1 = np.array(
            [0.5315275, 0.4819456, 0.4750305, 0.4453807, 0.44164768, 0.47079763, 0.40049344, 0.39453578, 0.47368276]
        )

        assert np.abs(image_slice0.flatten() - expected_slice0).max() < 1e-2
        assert np.abs(image_slice1.flatten() - expected_slice1).max() < 1e-2

    def test_stable_diffusion_controlnet_ddim_two_images_per_prompt(self):
        device = "cpu"  # ensure determinism for the device-dependent torch.Generator

        vae_scale_factor = 8
        components = self.get_dummy_components_for_controlnet()
        sd_pipe = StableDiffusionControlNetPipeline(**components)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs_for_controlnet(device, num_images_per_prompt=2)
        output = sd_pipe(**inputs)
        image = output.images

        image_slice0 = image[0, -3:, -3:, -1]
        image_slice1 = image[1, -3:, -3:, -1]

        # print("image_slice0", image_slice0)
        # print("image_slice1", image_slice1)

        assert image.shape == (2, 32 * vae_scale_factor, 32 * vae_scale_factor, 3)

        expected_slice0 = np.array(
            [0.44349974, 0.46209368, 0.4967181, 0.5238648, 0.5147134, 0.5299364, 0.47317895, 0.47206104, 0.48903918]
        )
        expected_slice1 = np.array(
            [0.5333272, 0.48134372, 0.47437134, 0.44782317, 0.44065917, 0.4701641, 0.40167314, 0.39400867, 0.47319612]
        )

        assert np.abs(image_slice0.flatten() - expected_slice0).max() < 1e-2
        assert np.abs(image_slice1.flatten() - expected_slice1).max() < 1e-2


@slow
@require_torch_gpu
class StableDiffusionControlNetPipelineSlowTests(unittest.TestCase):
    model_id = "takuma104/control_sd15_canny"
    controlnet_memsize = 1451078656  # in float32, https://gist.github.com/takuma104/ce954bde6511a1f0b031a87a646b1f7d

    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def get_inputs(self, device, generator_device="cpu", dtype=torch.float32, seed=0):
        generator = torch.Generator(device=generator_device).manual_seed(seed)
        latents = torch.randn((1, 4, 64, 64), generator=generator, dtype=dtype)
        vae_scale_factor = 8
        image = torch.randn((1, 3, 64 * vae_scale_factor, 64 * vae_scale_factor), generator=generator, dtype=dtype)
        inputs = {
            "prompt": "a photograph of an astronaut riding a horse",
            "latents": latents,
            "generator": generator,
            "num_inference_steps": 50,
            "guidance_scale": 7.5,
            "output_type": "numpy",
            "image": image,
        }
        return inputs

    def test_stable_diffusion_controlnet_ddim(self):
        sd_pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, safety_checker=None)
        sd_pipe.scheduler = DDIMScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        # print(image_slice)

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array(
            [1.0, 0.9598756, 0.8430315, 0.9999685, 0.9130426, 0.8025453, 0.87997377, 0.8080752, 0.7180274]
        )
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_stable_diffusion_controlnet_lms(self):
        sd_pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, safety_checker=None)
        sd_pipe.scheduler = LMSDiscreteScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        # print(image_slice)

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array(
            [1.0, 0.9631732, 0.84487236, 1.0, 0.914418, 0.8033508, 0.88200307, 0.809505, 0.7186936]
        )
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_stable_diffusion_controlnet_dpm(self):
        sd_pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, safety_checker=None)
        sd_pipe.scheduler = DPMSolverMultistepScheduler.from_config(sd_pipe.scheduler.config)
        sd_pipe = sd_pipe.to(torch_device)
        sd_pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device)
        image = sd_pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1].flatten()

        # print(image_slice)

        assert image.shape == (1, 512, 512, 3)
        expected_slice = np.array(
            [1.0, 0.9627134, 0.8445909, 1.0, 0.9132767, 0.8025819, 0.88159156, 0.8089917, 0.71824443]
        )
        assert np.abs(image_slice - expected_slice).max() < 1e-4

    def test_stable_diffusion_controlnet_attention_slicing(self):
        torch.cuda.reset_peak_memory_stats()
        pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        # enable attention slicing
        pipe.enable_attention_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        image_sliced = pipe(**inputs).images

        mem_bytes = torch.cuda.max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        # make sure that less than 3.75 GB is allocated
        assert mem_bytes < 3.75 * 10**9 + self.controlnet_memsize / 2

        # disable slicing
        pipe.disable_attention_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        image = pipe(**inputs).images

        # make sure that more than 3.75 GB is allocated
        mem_bytes = torch.cuda.max_memory_allocated()
        assert mem_bytes > 3.75 * 10**9 + self.controlnet_memsize / 2
        assert np.abs(image_sliced - image).max() < 1e-3

    def test_stable_diffusion_vae_slicing(self):
        torch.cuda.reset_peak_memory_stats()
        pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        # enable vae slicing
        pipe.enable_vae_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        inputs["prompt"] = [inputs["prompt"]] * 4
        inputs["latents"] = torch.cat([inputs["latents"]] * 4)
        image_sliced = pipe(**inputs).images

        mem_bytes = torch.cuda.max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()
        # make sure that less than 4 GB is allocated
        assert mem_bytes < 4e9 + self.controlnet_memsize / 2

        # disable vae slicing
        pipe.disable_vae_slicing()
        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        inputs["prompt"] = [inputs["prompt"]] * 4
        inputs["latents"] = torch.cat([inputs["latents"]] * 4)
        image = pipe(**inputs).images

        # make sure that more than 4 GB is allocated
        mem_bytes = torch.cuda.max_memory_allocated()
        assert mem_bytes > 4e9 + self.controlnet_memsize / 2
        # There is a small discrepancy at the image borders vs. a fully batched version.
        assert np.abs(image_sliced - image).max() < 1e-2

    def test_stable_diffusion_fp16_vs_autocast(self):
        # this test makes sure that the original model with autocast
        # and the new model with fp16 yield the same result
        pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        image_fp16 = pipe(**inputs).images

        with torch.autocast(torch_device):
            inputs = self.get_inputs(torch_device)
            image_autocast = pipe(**inputs).images

        # Make sure results are close enough
        diff = np.abs(image_fp16.flatten() - image_autocast.flatten())
        # They ARE different since ops are not run always at the same precision
        # however, they should be extremely close.
        assert diff.mean() < 2e-2

    def test_stable_diffusion_controlnet_intermediate_state(self):
        number_of_steps = 0

        def callback_fn(step: int, timestep: int, latents: torch.FloatTensor) -> None:
            callback_fn.has_been_called = True
            nonlocal number_of_steps
            number_of_steps += 1
            if step == 1:
                latents = latents.detach().cpu().numpy()
                assert latents.shape == (1, 4, 64, 64)
                latents_slice = latents[0, -3:, -3:, -1]
                expected_slice = np.array([-1.981, 1.052, -1.0625, -0.01709, -1.138, -0.592, -0.372, 0.332, 0.845])
                # print(latents_slice.flatten())
                assert np.abs(latents_slice.flatten() - expected_slice).max() < 5e-2
            elif step == 2:
                latents = latents.detach().cpu().numpy()
                assert latents.shape == (1, 4, 64, 64)
                latents_slice = latents[0, -3:, -3:, -1]
                expected_slice = np.array([-2.043, 1.113, -1.138, 0.062, -1.133, -0.614, -0.3901, 0.352, 0.8667])
                # print(latents_slice.flatten())
                assert np.abs(latents_slice.flatten() - expected_slice).max() < 5e-2

        callback_fn.has_been_called = False

        pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing()

        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        pipe(**inputs, callback=callback_fn, callback_steps=1)
        assert callback_fn.has_been_called
        assert number_of_steps == inputs["num_inference_steps"]

    def test_stable_diffusion_low_cpu_mem_usage(self):
        pipeline_id = self.model_id

        start_time = time.time()
        pipeline_low_cpu_mem_usage = StableDiffusionControlNetPipeline.from_pretrained(
            pipeline_id, torch_dtype=torch.float16
        )
        pipeline_low_cpu_mem_usage.to(torch_device)
        low_cpu_mem_usage_time = time.time() - start_time

        start_time = time.time()
        _ = StableDiffusionControlNetPipeline.from_pretrained(
            pipeline_id, torch_dtype=torch.float16, low_cpu_mem_usage=False
        )
        normal_load_time = time.time() - start_time

        assert 2 * low_cpu_mem_usage_time < normal_load_time

    def test_stable_diffusion_pipeline_with_sequential_cpu_offloading(self):
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, torch_dtype=torch.float16)
        pipe = pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        pipe.enable_attention_slicing(1)
        pipe.enable_sequential_cpu_offload()

        inputs = self.get_inputs(torch_device, dtype=torch.float16)
        _ = pipe(**inputs)

        mem_bytes = torch.cuda.max_memory_allocated()
        # make sure that less than 2.8 GB is allocated
        assert mem_bytes < 2.8 * 10**9 + self.controlnet_memsize / 2

    def test_stable_diffusion_pipeline_with_model_offloading(self):
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        inputs = self.get_inputs(torch_device, dtype=torch.float16)

        # Normal inference

        pipe = StableDiffusionControlNetPipeline.from_pretrained(
            self.model_id,
            torch_dtype=torch.float16,
        )
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)
        outputs = pipe(**inputs)
        mem_bytes = torch.cuda.max_memory_allocated()

        # With model offloading

        # Reload but don't move to cuda
        pipe = StableDiffusionControlNetPipeline.from_pretrained(
            self.model_id,
            torch_dtype=torch.float16,
        )

        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        pipe.enable_model_cpu_offload()
        pipe.set_progress_bar_config(disable=None)
        outputs_offloaded = pipe(**inputs)
        mem_bytes_offloaded = torch.cuda.max_memory_allocated()

        assert np.abs(outputs.images - outputs_offloaded.images).max() < 1e-3
        assert mem_bytes_offloaded < mem_bytes
        assert mem_bytes_offloaded < 3.5 * 10**9 + self.controlnet_memsize / 2
        for module in pipe.text_encoder, pipe.unet, pipe.vae, pipe.safety_checker:
            assert module.device == torch.device("cpu")

        # With attention slicing
        torch.cuda.empty_cache()
        torch.cuda.reset_max_memory_allocated()
        torch.cuda.reset_peak_memory_stats()

        pipe.enable_attention_slicing()
        _ = pipe(**inputs)
        mem_bytes_slicing = torch.cuda.max_memory_allocated()

        assert mem_bytes_slicing < mem_bytes_offloaded
        assert mem_bytes_slicing < 3 * 10**9 + self.controlnet_memsize / 2

    def test_stable_diffusion_no_safety_checker(self):
        pipe = StableDiffusionControlNetPipeline.from_pretrained(self.model_id, safety_checker=None)
        assert isinstance(pipe, StableDiffusionControlNetPipeline)
        assert isinstance(pipe.scheduler, DDIMScheduler)
        assert pipe.safety_checker is None

        image = pipe("example prompt", num_inference_steps=2).images[0]
        assert image is not None

        # check that there's no error when saving a pipeline with one of the models being None
        with tempfile.TemporaryDirectory() as tmpdirname:
            pipe.save_pretrained(tmpdirname)
            pipe = StableDiffusionControlNetPipeline.from_pretrained(tmpdirname)

        # sanity check that the pipeline still works
        assert pipe.safety_checker is None
        image = pipe("example prompt", num_inference_steps=2).images[0]
        assert image is not None
