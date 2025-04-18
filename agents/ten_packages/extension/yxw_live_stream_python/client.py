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
        self.ten_env.log_debug(f"session_id:{self.session_id},rtmp_addr:{self.rtmp_addr}")

    async def start_ffmpeg(self) -> None:
        """启动 ffmpeg 进程读取 RTMP 流"""
        if not self.rtmp_addr:
            raise Exception("RTMP address not available")
            
        # 使用 ffmpeg 读取 RTMP 流
        # -i: 输入流
        # -f: 输出格式
        # -acodec: 音频编码器
        # -vcodec: 视频编码器
        # -ar: 音频采样率
        # -ac: 音频通道数
        # -s: 视频分辨率
        cmd = [
            'ffmpeg',
            '-i', self.rtmp_addr,
            '-f', 's16le',  # 音频输出格式
            '-acodec', 'pcm_s16le',  # 音频编码器
            '-ar', '16000',  # 音频采样率
            '-ac', '1',  # 单声道
            '-f', 'rawvideo',  # 视频输出格式
            '-vcodec', 'rawvideo',  # 视频编码器
            '-s', '1920x1080',  # 视频分辨率
            '-pix_fmt', 'rgba',  # 像素格式
            '-'
        ]
        
        self.ffmpeg_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**8
        )
        
        self.is_running = True
        asyncio.create_task(self._read_ffmpeg_output())

    async def _read_ffmpeg_output(self) -> None:
        """读取 ffmpeg 输出并分离音视频数据"""
        try:
            while self.is_running and self.ffmpeg_process and self.ffmpeg_process.poll() is None:
                # 读取音频数据 (16000Hz, 16bit, 单声道)
                audio_data = self.ffmpeg_process.stdout.read(320)  # 10ms 的音频数据
                if audio_data:
                    await self.audio_queue.put(audio_data)
                
                # 读取视频数据 (1920x1080 RGBA)
                video_data = self.ffmpeg_process.stdout.read(1920 * 1080 * 4)
                if video_data:
                    await self.video_queue.put(video_data)
        except Exception as e:
            print(f"Error reading ffmpeg output: {e}")
        finally:
            self.is_running = False

    async def read_audio_frame(self) -> AsyncGenerator[bytes, None]:
        """读取音频帧"""
        while self.is_running:
            try:
                audio_data = await self.audio_queue.get()
                yield audio_data
            except Exception as e:
                print(f"Error reading audio frame: {e}")
                break

    async def read_video_frame(self) -> AsyncGenerator[bytes, None]:
        """读取视频帧"""
        while self.is_running:
            try:
                video_data = await self.video_queue.get()
                yield video_data
            except Exception as e:
                print(f"Error reading video frame: {e}")
                break

    async def start(self) -> None:
        """启动客户端，包括创建会话、等待会话就绪、启动会话和连接 WebSocket"""
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
        
        # 启动 ffmpeg 读取 RTMP 流
        await self.start_ffmpeg()

    async def close(self) -> None:
        """关闭客户端，包括关闭 WebSocket 连接和会话"""
        self.is_running = False
        
        if self.ffmpeg_process:
            self.ffmpeg_process.terminate()
            try:
                self.ffmpeg_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None
            
        await self.close_websocket()
        await self.close_session()
        self.session_id = None
        self.rtmp_addr = None

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
            
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "SessionId": self.session_id,
                "Command": "SEND_AUDIO",
                "Data": {
                    "Audio": base64.b64encode(audio_data).decode('utf-8'),
                    "Seq": seq,
                    "IsFinal": is_final
                }
            }
        }
        
        await self.websocket.send(json.dumps(params))

    async def send_audio_finish(self, seq: int) -> None:
        """发送音频结束标记"""
        if not self.websocket:
            raise Exception("WebSocket 未连接")
            
        params = {
            'Header': {},
            'Payload': {
                "ReqId": self._get_uuid(),
                "SessionId": self.session_id,
                "Command": "SEND_AUDIO",
                "Data": {
                    "Audio": "",
                    "Seq": seq,
                    "IsFinal": True
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