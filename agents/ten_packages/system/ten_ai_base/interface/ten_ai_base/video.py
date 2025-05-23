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
from .const import CMD_IN_FLUSH, CMD_OUT_FLUSH, DATA_IN_PROPERTY_END_OF_SEGMENT, DATA_IN_PROPERTY_TEXT, DATA_IN_PROPERTY_QUIET
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
        self.lock = asyncio.Lock()
        self.queue = AsyncQueue()
        self.current_task = None
        self.loop_task = None

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        await super().on_start(ten_env)
        if self.loop_task is None:
            self.loop = asyncio.get_event_loop()
            self.loop_task = self.loop.create_task(
                self._process_queue(ten_env))

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        await super().on_stop(ten_env)
        self.loop_task.cancel()

    async def on_deinit(self, ten_env: AsyncTenEnv) -> None:
        await super().on_deinit(ten_env)
    
    async def on_cmd(self, async_ten_env: AsyncTenEnv, cmd: Cmd) -> None:
        cmd_name = cmd.get_name()
        async_ten_env.log_info(f"on_cmd name: {cmd_name}")
        if cmd_name == CMD_IN_FLUSH:
            await self.on_cancel_steam(async_ten_env)
            await self.flush_input_items(async_ten_env)
            await async_ten_env.send_cmd(Cmd.create(CMD_OUT_FLUSH))
            async_ten_env.log_info("on_cmd sent flush")
            status_code, detail = StatusCode.OK, "success"
            cmd_result = CmdResult.create(status_code)
            cmd_result.set_property_string("detail", detail)
            await async_ten_env.return_result(cmd_result, cmd)
        else:
            status_code, detail = StatusCode.OK, "success"
            cmd_result = CmdResult.create(status_code)
            cmd_result.set_property_string("detail", detail)
            await async_ten_env.return_result(cmd_result, cmd)
    
    async def flush_input_items(self, ten_env: AsyncTenEnv):
        """Flushes the self.queue and cancels the current task."""
        # Flush the queue using the new flush method
        await self.queue.flush()
        # Cancel the current task if one is running
        if self.current_task:
            ten_env.log_info("Cancelling the current task during flush.")
            self.current_task.cancel()
    
    @abstractmethod
    async def on_cancel_steam(self, ten_env: AsyncTenEnv) -> None:
        """Called when the TTS request is cancelled."""
        pass

    async def send_audio_out(self, ten_env: AsyncTenEnv, audio_data: bytes,timestamp:int, **args: TTSPcmOptions) -> None:
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
                f.set_timestamp(timestamp)
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

    async def send_video_out(self, ten_env: AsyncTenEnv, video_data: bytes, width: int, height: int,timestamp:int, format: str = "rgba") -> None:
        """Send video data to output."""
        try:
            f = VideoFrame.create("video_frame")
            f.set_timestamp(timestamp)
            f.set_width(width)
            f.set_height(height)
            f.set_pixel_fmt(PixelFmt.RGBA if format == "rgba" else PixelFmt.I420)
            f.alloc_buf(len(video_data))
            buff = f.lock_buf()
            buff[:] = video_data
            f.unlock_buf(buff)
            await ten_env.send_video_frame(f)
        except Exception as e:
            ten_env.log_error(f"error sending video frame: {traceback.format_exc()}") 
        
    async def on_audio_frame(self, ten_env: AsyncTenEnv, frame: AudioFrame) -> None:
        """处理音频帧"""
        async with self.lock:
            try:
                frame_buf = frame.get_buf()
                if frame.get_timestamp() == -1:
                    await self.queue.put(bytearray())
                    return
                if not frame_buf:
                    ten_env.log_info("empty audio frame detected")
                    return
                await self.queue.put(frame_buf)
            
            except Exception as e:
                ten_env.log_error(f"error processing audio frame: {traceback.format_exc()}") 
    
    async def _process_queue(self, ten_env: AsyncTenEnv):
        """Asynchronously process queue items one by one."""
        while True:
            # Wait for an item to be available in the queue
            audio = await self.queue.get()
            try:
                self.current_task = asyncio.create_task(
                    self.process_audio(ten_env=ten_env,audio=audio))
                await self.current_task  # Wait for the current task to finish or be cancelled
                self.current_task = None
            except asyncio.CancelledError:
                ten_env.log_info(f"Task cancelled")
            except Exception as err:
                ten_env.log_error(
                    f"Task failed, err: {traceback.format_exc()}")
    
    @abstractmethod
    async def process_audio(self, ten_env: AsyncTenEnv, audio:bytearray) -> None:
        """
        Called when a new input item is available in the queue. Override this method to implement the TTS request logic.
        Use send_audio_out to send the audio data to the output when the audio data is ready.
        """
        pass