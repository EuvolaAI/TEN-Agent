from ten import (
    AsyncTenEnv
)
import hmac
import hashlib
import base64
from urllib.parse import urlencode
import time
import requests
import json
import uuid
import websockets
import asyncio
import subprocess
import numpy as np
from typing import Optional, Tuple, AsyncGenerator
from typing import Dict, Union, Optional, cast, Any, Callable
import signal

class YxwLiveStreamClient:
    def __init__(self,ten_env: AsyncTenEnv, app_key: str, access_token: str, virtualman_key: str):
        self.ten_env = ten_env
        self.app_key = app_key
        self.access_token = access_token
        self.virtualman_key = virtualman_key
        self.encoder = 'utf-8'
        self.base_url = "https://gw.tvs.qq.com"
        self.session_id: Optional[str] = None
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self.rtmp_addr: Optional[str] = None
        self.ffmpeg_process: Optional[subprocess.Popen] = None
        self.audio_queue: asyncio.Queue = asyncio.Queue()
        self.video_queue: asyncio.Queue = asyncio.Queue()
        self.is_running = False
        self.loop = None
        self._on_open:Callable = None
        self._on_close:Callable = None
        self._on_error:Callable = None
        self._on_transcript:Callable = None
        # self._on_receive_audio:Callable = None
    
    def on(self,on_open:Callable,on_close:Callable,on_error:Callable,on_transcript:Callable):
        self._on_open = on_open
        self._on_close = on_close
        self._on_error = on_error
        self._on_transcript = on_transcript
        # self._on_receive_audio = on_receive_audio

    def _query_string(self) -> str:
        timestamp = str(int(time.time()))
        appkey_timestamp = f"appkey={self.app_key}&timestamp={timestamp}"
        hash = hmac.new(
            self.access_token.encode(self.encoder),
            appkey_timestamp.encode(self.encoder),
            hashlib.sha256
        ).digest()
        signature = base64.b64encode(hash)
        dic = {"signature": signature}
        signature = urlencode(dic)
        return f"?{appkey_timestamp}&{signature}"

    def _websocket_string(self, session_id: str) -> str:
        timestamp = str(int(time.time()))
        appkey_timestamp = f"appkey={self.app_key}&requestid={session_id}&timestamp={timestamp}"
        hash = hmac.new(
            self.access_token.encode(self.encoder),
            appkey_timestamp.encode(self.encoder),
            hashlib.sha256
        ).digest()
        signature = base64.b64encode(hash)
        dic = {"signature": signature}
        signature = urlencode(dic)
        return f"?{appkey_timestamp}&{signature}"

    def _get_uuid(self) -> str:
        return "".join(str(uuid.uuid4()).split("-"))

    def _headers(self) -> dict:
        return {"Content-Type": "application/json;charset=UTF-8"}

    async def create_session(self) -> None:
        """创建会话"""
        api = "/v2/ivh/sessionmanager/sessionmanagerservice/createsessionbyasset"
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "AssetVirtualmanKey": self.virtualman_key,
                "DriverType": 3,  # 音频驱动
                "UserId": "aibum",
                "Protocol": "rtmp",
            }
        }
        
        response = requests.post(
            self.base_url + api + self._query_string(),
            headers=self._headers(),
            data=json.dumps(params)
        )
        
        result = response.json()
        if result.get('Header', {}).get('Code') != 0:
            raise Exception(f"创建会话失败: {result}")
            
        payload = result.get('Payload', {})
        self.session_id = payload.get('SessionId')
        self.rtmp_addr = payload.get('PlayStreamAddr')
        self.ten_env.log_info(f"session_id:{self.session_id},rtmp_addr:{self.rtmp_addr}")

    async def start_ffmpeg(self) -> None:
        """启动 ffmpeg 进程读取 RTMP 流"""
        if not self.rtmp_addr:
            raise Exception("RTMP address not available")
            
        # 创建两个管道
        audio_pipe = subprocess.PIPE
        video_pipe = subprocess.PIPE
        
        cmd = [
            'ffmpeg',
            '-i', self.rtmp_addr,
            '-map', '0:a',  # 音频流
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ar', '16000',
            '-ac', '1',
            'pipe:1',  # 音频输出到管道1
            '-map', '0:v',  # 视频流
            '-f', 'h264',   # 输出H.264裸流
            '-vcodec', 'copy',  # 直接复制视频流，不重新编码
            'pipe:2'  # 视频输出到管道2
        ]
        
        self.ffmpeg_process = subprocess.Popen(
            cmd,
            stdout=audio_pipe,
            stderr=video_pipe,
            bufsize=50 * 1024 * 1024
        )
        
        self.is_running = True
        asyncio.create_task(self._read_audio_output())
        asyncio.create_task(self._read_video_output())

    async def _read_audio_output(self) -> None:
        """读取音频数据"""
        while self.is_running:
            if not self.ffmpeg_process:
                self.ten_env.log_error("FFmpeg 进程不存在")
                break
            if self.ffmpeg_process.poll() is not None:
                self.ten_env.log_error(f"FFmpeg 进程已退出，返回码: {self.ffmpeg_process.poll()}")
                break
            audio_data = await asyncio.get_event_loop().run_in_executor(
                None, 
                self.ffmpeg_process.stdout.read, 
                1600
            ) # 100ms 的音频数据
            # self.ten_env.log_info(f'_read_audio_output,len:{len(audio_data)}')
            if audio_data:
                await self.audio_queue.put(audio_data)

    async def _read_video_output(self) -> None:
        """读取视频数据"""
        while self.is_running:
            if not self.ffmpeg_process:
                self.ten_env.log_error("FFmpeg 进程不存在")
                break
            if self.ffmpeg_process.poll() is not None:
                self.ten_env.log_error(f"FFmpeg 进程已退出，返回码: {self.ffmpeg_process.poll()}")
                break
            video_data = await asyncio.get_event_loop().run_in_executor(
                None, 
                self.ffmpeg_process.stderr.read, 
                1024 * 1024
            ) # 每次读取1MB
            self.ten_env.log_info(f'_read_video_output,len:{len(video_data)}')
            if video_data:
                await self.video_queue.put(video_data)

    async def read_audio_frame(self) -> bytes:
        """读取音频帧"""
        try:
            audio_data = await self.audio_queue.get()
            return audio_data
        except Exception as e:
            print(f"Error reading audio frame: {e}")

    async def read_video_frame(self) -> bytes:
        """读取视频帧"""
        while self.is_running:
            try:
                video_data = await self.video_queue.get()
                return video_data
            except Exception as e:
                print(f"Error reading video frame: {e}")
                break

    async def start(self) -> None:
        """启动客户端，包括创建会话、等待会话就绪、启动会话和连接 WebSocket"""
        self.loop = asyncio.get_event_loop()  
        # 创建会话
        await self.create_session()
        
        # 等待会话就绪
        while True:
            status = await self.query_session_status()
            if status == 1:  # 会话就绪
                break
            await asyncio.sleep(1)
            
        # 启动会话
        await self.start_session()
        
        # 连接 WebSocket
        await self.connect_websocket()
        if self._on_open is not None:
            await self._on_open()

        self.loop.create_task(self.receive())
        # 启动 ffmpeg 读取 RTMP 流
        await self.start_ffmpeg()

    async def close(self) -> None:
        """关闭客户端，包括关闭 WebSocket 连接和会话"""
        self.is_running = False
        
        if self.ffmpeg_process:
            self.ffmpeg_process.send_signal(signal.SIGINT)
            try:
                self.ffmpeg_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None
            
        await self.close_websocket()
        await self.close_session()
        self.session_id = None
        self.rtmp_addr = None
        if self._on_close is not None:
            await self._on_close()

    async def query_session_status(self) -> int:
        """查询会话状态"""
        if not self.session_id:
            raise Exception("会话未创建")
            
        api = "/v2/ivh/sessionmanager/sessionmanagerservice/statsession"
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "SessionId": self.session_id,
            }
        }
        
        response = requests.post(
            self.base_url + api + self._query_string(),
            headers=self._headers(),
            data=json.dumps(params)
        )
        
        result = response.json()
        if result.get('Header', {}).get('Code') != 0:
            raise Exception(f"查询会话状态失败: {result}")
            
        return result.get('Payload', {}).get('SessionStatus')

    async def start_session(self) -> None:
        """启动会话"""
        if not self.session_id:
            raise Exception("会话未创建")
            
        api = "/v2/ivh/sessionmanager/sessionmanagerservice/startsession"
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "SessionId": self.session_id,
            }
        }
        
        response = requests.post(
            self.base_url + api + self._query_string(),
            headers=self._headers(),
            data=json.dumps(params)
        )
        
        result = response.json()
        if result.get('Header', {}).get('Code') != 0:
            raise Exception(f"启动会话失败: {result}")

    async def close_session(self) -> None:
        """关闭会话"""
        if not self.session_id:
            return
            
        api = "/v2/ivh/sessionmanager/sessionmanagerservice/closesession"
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "SessionId": self.session_id,
            }
        }
        
        response = requests.post(
            self.base_url + api + self._query_string(),
            headers=self._headers(),
            data=json.dumps(params)
        )
        
        result = response.json()
        self.ten_env.log_info(result.json())
        if result.get('Header', {}).get('Code') != 0:
            raise Exception(f"关闭会话失败: {result}")

    async def connect_websocket(self) -> None:
        """连接 WebSocket"""
        if not self.session_id:
            raise Exception("会话未创建")
            
        api = "wss://gw.tvs.qq.com/v2/ws/ivh/interactdriver/interactdriverservice/commandchannel"
        uri = api + self._websocket_string(self.session_id)
        self.websocket = await websockets.connect(uri)

    async def send_audio_message(self, audio_data: bytes, seq: int, is_final: bool = False) -> None:
        """发送音频消息"""
        if not self.websocket:
            raise Exception("WebSocket 未连接")
        audio = ""
        if is_final:
            audio = ""
        else :
            audio = base64.b64encode(audio_data).decode('utf-8')
            
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "SessionId": self.session_id,
                "Command": "SEND_AUDIO",
                "Data": {
                    "Audio": audio,
                    "Seq": seq,
                    "IsFinal": is_final
                }
            }
        }
        
        await self.websocket.send(json.dumps(params))

    async def close_websocket(self) -> None:
        """关闭 WebSocket 连接"""
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出"""
        await self.close() 

    async def receive(self):
        while True:
            try:
                message = await self.websocket.recv()
                
                if self._on_transcript is not None:
                    await self._on_transcript(message)
            except websockets.exceptions.ConnectionClosed as e:
                print("连接已关闭")
                if self._on_error is not None:
                    await self._on_error(e)
                break
            except Exception as e:
                print(f"接收消息时发生错误: {e}")
                if self._on_error is not None:
                    await self._on_error(e)
                break