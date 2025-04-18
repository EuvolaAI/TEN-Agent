#
# This file is part of TEN Framework, an open source project.
# Licensed under the Apache License, Version 2.0.
# See the LICENSE file for more information.
#
from ten import (
    AsyncTenEnv,
    AudioFrame,
    Cmd,
    StatusCode,
    CmdResult,
)
import traceback
from ten_ai_base.video import AsyncVideoBaseExtension
from typing import Optional
from ten_ai_base.config import BaseConfig
from .client import YxwLiveStreamClient
import asyncio
import time
from dataclasses import dataclass

@dataclass
class YxwLiveStreamConfig(BaseConfig):
    app_key: str = ""
    access_token: str = ""
    virtualman_key: str = ""

class YxwLiveStreamExtension(AsyncVideoBaseExtension):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.config: YxwLiveStreamConfig = None
        self.client: YxwLiveStreamClient = None
        self.seq = 0
        self.audio_task = None
        self.video_task = None
        self.frame_buff = bytearray()
        self.last_send_time = 0
        self.last_data_time = 0  # 上次收到有效数据的时间
        self.SEGMENT_SIZE = 5120  # 160ms * 16000Hz * 2bytes * 1channel / 1000
        self.MIN_INTERVAL = 120  # 最小发送间隔120ms
        self.SILENCE_TIMEOUT = 800  # 800ms无数据认为说话结束

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        ten_env.log_debug("on_init")

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        try:
            await super().on_start(ten_env)
            ten_env.log_debug("on_start")
            
            self.config = await YxwLiveStreamConfig.create_async(ten_env=ten_env)
            ten_env.log_info(f"yxw_live_stream config: {self.config}")
            if not self.config.app_key:
                raise ValueError("app_key is required")
            if not self.config.access_token:
                raise ValueError("access_token is required")
            if not self.config.virtualman_key:
                raise ValueError("virtualman_key is required")
                
            self.client = YxwLiveStreamClient(
                ten_env=ten_env,
                app_key=self.config.app_key,
                access_token=self.config.access_token,
                virtualman_key=self.config.virtualman_key
            )
            
            await self.client.start()
            
            # 启动读取音视频数据的任务
            self.audio_task = asyncio.create_task(self._read_audio_frames(ten_env))
            self.video_task = asyncio.create_task(self._read_video_frames(ten_env))
            
        except Exception:
            ten_env.log_error(f"on_start failed: {traceback.format_exc()}")

    async def _read_audio_frames(self, ten_env: AsyncTenEnv) -> None:
        """读取音频帧并发送"""
        try:
            async for audio_data in self.client.read_audio_frame():
                await self.send_audio_out(ten_env, audio_data)
        except Exception as e:
            ten_env.log_error(f"Error reading audio frames: {traceback.format_exc()}")

    async def _read_video_frames(self, ten_env: AsyncTenEnv) -> None:
        """读取视频帧并发送"""
        try:
            async for video_data in self.client.read_video_frame():
                await self.send_video_out(ten_env, video_data, width=1920, height=1080)
        except Exception as e:
            ten_env.log_error(f"Error reading video frames: {traceback.format_exc()}")

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        if self.audio_task:
            self.audio_task.cancel()
            try:
                await self.audio_task
            except asyncio.CancelledError:
                pass
                
        if self.video_task:
            self.video_task.cancel()
            try:
                await self.video_task
            except asyncio.CancelledError:
                pass
                
        if self.client:
            await self.client.close()
            self.client = None

        await super().on_stop(ten_env)
        ten_env.log_debug("on_stop")

    async def on_deinit(self, ten_env: AsyncTenEnv) -> None:
        await super().on_deinit(ten_env)
        ten_env.log_debug("on_deinit")

    async def on_audio_frame(self, ten_env: AsyncTenEnv, frame: AudioFrame) -> None:
        """处理音频帧"""
        try:
            frame_buf = frame.get_buf()
            if not frame_buf:
                ten_env.log_warn("empty audio frame detected")
                return

            current_time = time.time() * 1000  # 转换为毫秒
            
            # 检查是否超时
            if current_time - self.last_data_time > self.SILENCE_TIMEOUT and len(self.frame_buff) == 0:
                # 检查 WebSocket 连接状态
                if self.client and self.client.websocket and not self.client.websocket.closed:
                    # 发送结束包
                    await self.client.send_audio_finish(self.seq)
                    self.seq = 0
                return

            # 更新最后数据时间
            self.last_data_time = current_time

            # 将新数据添加到缓冲区
            self.frame_buff.extend(frame_buf)
            
            # 如果缓冲区数据足够一个分片，且满足发送间隔要求
            while len(self.frame_buff) >= self.SEGMENT_SIZE:
                # 检查发送间隔
                if current_time - self.last_send_time < self.MIN_INTERVAL:
                    break
                    
                # 获取一个分片
                segment = bytes(self.frame_buff[:self.SEGMENT_SIZE])
                self.frame_buff = self.frame_buff[self.SEGMENT_SIZE:]
                
                # 检查 WebSocket 连接状态
                if self.client and self.client.websocket and not self.client.websocket.closed:
                    # 发送音频数据
                    await self.client.send_audio_message(segment, self.seq)
                    self.seq += 1
                    
                    # 更新最后发送时间
                    self.last_send_time = current_time
            
        except Exception as e:
            ten_env.log_error(f"error processing audio frame: {traceback.format_exc()}") 