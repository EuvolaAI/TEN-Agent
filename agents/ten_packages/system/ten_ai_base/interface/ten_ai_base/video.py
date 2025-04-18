#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from abc import ABC, abstractmethod
import asyncio
import traceback
from .types import TTSPcmOptions

from ten import (
    AsyncExtension,
    Data,
)
from ten.async_ten_env import AsyncTenEnv
from ten.audio_frame import AudioFrame, AudioFrameDataFmt
from ten.video_frame import VideoFrame, PixelFmt
from ten.cmd import Cmd
from ten.cmd_result import CmdResult, StatusCode
from .helper import AsyncQueue, get_property_bool, get_property_string

class AsyncVideoBaseExtension(AsyncExtension, ABC):
    """
    Base class for implementing a Video Processing Extension.
    This class provides a basic implementation for processing video output.
    Use send_video_out to send the video data to the output.
    """
    def __init__(self, name: str):
        super().__init__(name)
        self.leftover_video_bytes = b''
        self.leftover_audio_bytes = b''

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        await super().on_start(ten_env)

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        await super().on_stop(ten_env)

    async def on_deinit(self, ten_env: AsyncTenEnv) -> None:
        await super().on_deinit(ten_env)

    async def send_audio_out(self, ten_env: AsyncTenEnv, audio_data: bytes, **args: TTSPcmOptions) -> None:
        """Send audio data to output."""
        sample_rate = args.get("sample_rate", 16000)
        bytes_per_sample = args.get("bytes_per_sample", 2)
        number_of_channels = args.get("number_of_channels", 1)
        try:
            # Combine leftover bytes with new audio data
            combined_data = self.leftover_audio_bytes + audio_data

            # Check if combined_data length is odd
            if len(combined_data) % (bytes_per_sample * number_of_channels) != 0:
                # Save the last incomplete frame
                valid_length = len(combined_data) - (len(combined_data) % (bytes_per_sample * number_of_channels))
                self.leftover_audio_bytes = combined_data[valid_length:]
                combined_data = combined_data[:valid_length]
            else:
                self.leftover_audio_bytes = b''

            if combined_data:
                f = AudioFrame.create("pcm_frame")
                f.set_sample_rate(sample_rate)
                f.set_bytes_per_sample(bytes_per_sample)
                f.set_number_of_channels(number_of_channels)
                f.set_data_fmt(AudioFrameDataFmt.INTERLEAVE)
                f.set_samples_per_channel(len(combined_data) // (bytes_per_sample * number_of_channels))
                f.alloc_buf(len(combined_data))
                buff = f.lock_buf()
                buff[:] = combined_data
                f.unlock_buf(buff)
                await ten_env.send_audio_frame(f)
        except Exception as e:
            ten_env.log_error(f"error sending audio frame: {traceback.format_exc()}")

    async def send_video_out(self, ten_env: AsyncTenEnv, video_data: bytes, width: int, height: int, format: str = "rgba") -> None:
        """Send video data to output."""
        try:
            f = VideoFrame.create("video_frame")
            f.set_width(width)
            f.set_height(height)
            f.set_data_fmt(PixelFmt.PixelFmtRGBA if format == "rgba" else PixelFmt.I420)
            f.alloc_buf(len(video_data))
            buff = f.lock_buf()
            buff[:] = video_data
            f.unlock_buf(buff)
            await ten_env.send_video_frame(f)
        except Exception as e:
            ten_env.log_error(f"error sending video frame: {traceback.format_exc()}") 