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
from ten.video_frame import VideoFrame, PixelFmt
import asyncio
import time
from dataclasses import dataclass
import uuid

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
        self.frame_lenth = 0
        self.req_id:str = ""

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        self.ten_env=ten_env
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
        async def on_open():
            self.ten_env.log_info(f"yxw_live_stream on_open")
            self.connected = True

        async def on_close():
            self.ten_env.log_info(f"yxw_live_stream on_close")

        async def on_message(result):
            self.ten_env.log_info(f"yxw_live_stream on_message:{result}")

        async def on_error(error):
            import traceback
            self.ten_env.log_error(traceback.format_exc())
            self.ten_env.log_error(f"yxw_live_stream on_error: {error}")

        # connect to websocket
        self.client.on(on_open = on_open,on_close=on_close,on_error=on_error,on_transcript=on_message)

        await self.client.start()

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        try:
            await super().on_start(ten_env)
            ten_env.log_debug("on_start")
            
            # 启动同步读取音视频数据的任务
            self.av_task = asyncio.create_task(self._read_av_frames(ten_env))
            
        except Exception:
            ten_env.log_error(f"on_start failed: {traceback.format_exc()}")

    async def _read_av_frames(self, ten_env: AsyncTenEnv) -> None:
        """同步读取和处理音视频帧"""
        try:
            while self.client.is_running:
                start_time = time.time_ns()
                try:
                    # 同步读取音视频帧
                    audio_data = await self.client.read_audio_frame()
                    video_data = await self.client.read_video_frame()
                    
                    if not audio_data or not video_data:
                        ten_env.log_warn("收到空音视频数据")
                        continue
                    
                    # 计算时间戳（纳秒）
                    timestamp = self.client.start_time + (self.client.audio_frame_count * self.client.frame_interval)
                    
                    # 发送音频帧
                    # ten_env.log_info(f"准备发送audio帧: {len(audio_data)} 字节")
                    await self.send_audio_out(ten_env, audio_data, timestamp)
                    self.client.audio_frame_count += 1
                    
                    # 发送视频帧
                    # ten_env.log_info(f"准备发送video帧: {len(video_data)} 字节")
                    await self.send_video_out(
                        ten_env=ten_env,
                        video_data=video_data,
                        width=self.client.width,
                        height=self.client.height,
                        timestamp=timestamp,
                        format="I420"
                    )
                    self.client.video_frame_count += 1
                    
                    # 计算发送耗时和等待时间
                    consume_time = time.time_ns() - start_time
                    wait_time = max(0, (self.client.frame_interval - consume_time) / 1_000_000_000)
                    if wait_time > 0:
                        await asyncio.sleep(wait_time)
                        
                except Exception as e:
                    ten_env.log_error(f"处理音视频帧时出错: {str(e)}")
                    continue
                    
        except Exception as e:
            ten_env.log_error(f"音视频帧读取任务出错: {str(e)}")
        finally:
            ten_env.log_info("音视频帧读取任务结束")

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        await super().on_stop(ten_env)
        if self.av_task:
            self.av_task.cancel()
            try:
                await self.av_task
            except asyncio.CancelledError:
                pass
                
        # if self.video_task:
        #     self.video_task.cancel()
        #     try:
        #         await self.video_task
        #     except asyncio.CancelledError:
        #         pass
        ten_env.log_info("yxw_live_stream on_stop")
        if self.client:
            await self.client.close()
            self.client = None
        ten_env.log_debug("on_stop")

    async def on_deinit(self, ten_env: AsyncTenEnv) -> None:
        await super().on_deinit(ten_env)
        ten_env.log_debug("on_deinit")
        
    async def process_audio(self, ten_env: AsyncTenEnv, audio:bytearray) -> None:
        try:
            # # 将PCM音频数据写入到文件
            # try:
            #     # 使用追加模式打开文件，确保数据被添加到文件末尾
            #     # 频繁打开关闭文件可能导致性能问题，但在这种情况下是必要的
            #     # 因为我们需要确保每帧数据都被正确写入，即使程序意外终止
            #     with open('/app/audio.pcm', 'ab') as audio_file:
            #             # 写入当前帧的音频数据
            #             audio_file.write(audio)
            #             ten_env.log_info(f'已将 {len(audio)} 字节的PCM数据追加到 /app/audio.pcm')
            # except Exception as e:
            #     ten_env.log_error(f"写入音频数据到文件时出错: {traceback.format_exc()}")
            # current_time = time.time() * 1000  # 转换为毫秒
            # 更新最后数据时间
            self.frame_lenth += len(audio)
            # self.last_data_time = current_time
            self.frame_buff.extend(audio)

            if len(audio) == 0:
                self.seq +=1
                await self.client.send_audio_message(
                    req_id=self.req_id,
                    audio_data=bytes(self.frame_buff[:]),
                    seq=self.seq,
                    is_final=False,
                )
                ten_env.log_info(f'send pcm data finished')
                await self.client.send_audio_message(
                    req_id=self.req_id,
                    audio_data=bytes(bytearray()),
                    seq=self.seq,
                    is_final=True,
                )
                self.frame_buff = []
                self.frame_lenth = 0
                self.req_id = ""
                return
            if self.req_id == "":
                self.req_id = self._get_uuid()
            
            # 循环处理缓冲区数据
            while len(self.frame_buff) >= 5120:
                if self.client and self.client.websocket and not self.client.websocket.closed:
                    self.seq += 1
                    ten_env.log_info(f'process_audio: 发送 5120 字节')
                    await self.client.send_audio_message(
                        req_id=self.req_id,
                        audio_data=bytes(self.frame_buff[:5120]), 
                        seq=self.seq,
                        is_final=False
                    )
                    # 保留剩余数据
                    self.frame_buff = self.frame_buff[5120:]
                    self.frame_lenth = len(self.frame_buff)
                    # self.last_send_time = current_time
                    # 强制等待 120ms
                    await asyncio.sleep(0.12)
        except Exception as e:
            ten_env.log_error(f"error processing audio frame: {traceback.format_exc()}") 

    async def on_cancel_steam(self, ten_env: AsyncTenEnv) -> None:
        # 打断
        ten_env.log_info(f'打断stream')
        await self.client.send_interrupt()
        self.req_id = ""
    
    def _get_uuid(self) -> str:
        return "".join(str(uuid.uuid4()).split("-"))