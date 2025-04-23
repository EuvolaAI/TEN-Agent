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
        self.ffmpeg_video_process: Optional[subprocess.Popen] = None
        self.audio_queue: asyncio.Queue = asyncio.Queue(maxsize=30)
        self.video_queue: asyncio.Queue = asyncio.Queue(maxsize=30)
        self.is_running = False
        self.loop = None
        self._on_open:Callable = None
        self._on_close:Callable = None
        self._on_error:Callable = None
        self._on_transcript:Callable = None
        self.start_time = time.time_ns()  # 记录开始时间
        self.audio_frame_count = 0
        self.video_frame_count = 0
        self.sample_rate = 16000
        self.bytes_per_sample = 2
        self.number_of_channels = 1
        self.width = 540
        self.height = 960
        self.frame_rate = 25
        self.frame_interval =int(1000 * 1000 * 1000 / self.frame_rate)
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
        
        cmd_audio = [
            'ffmpeg',
            '-i', self.rtmp_addr,
            '-map', '0:a',  # 音频流
            '-f', 's16le',
            '-acodec', 'pcm_s16le',
            '-ar', f'{self.sample_rate}',
            '-ac', f'{self.number_of_channels}',
            'pipe:1',  # 音频输出到管道1
        ]

        cmd_video = [
            'ffmpeg',
            '-i', self.rtmp_addr,
            # 视频输出
            '-map', '0:v',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', f'{self.width}x{self.height}',
            '-r', f'{self.frame_rate}',  # 匹配源视频帧率
            'pipe:1'  # 直接输出到文件
        ]
        
        self.ffmpeg_process = subprocess.Popen(
            cmd_audio,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10 * 1024 * 1024
        )

        self.ffmpeg_video_process = subprocess.Popen(
            cmd_video,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=50 * 1024 * 1024
        )

        self.is_running = True
        asyncio.create_task(self._read_av_output())

    async def _read_av_output(self) -> None:
        """同步读取音频和视频数据"""
        try:
            frame_size = int(self.width * self.height * 1.5)  # YUV420P总大小
            audio_frame_size = int(self.frame_interval * self.number_of_channels * self.sample_rate * self.bytes_per_sample / 1000 / 1000 / 1000)
            
            while self.is_running:
                # 检查进程状态
                if not self.ffmpeg_process or not self.ffmpeg_video_process:
                    self.ten_env.log_error("FFmpeg 进程不存在")
                    break
                    
                if self.ffmpeg_process.poll() is not None or self.ffmpeg_video_process.poll() is not None:
                    self.ten_env.log_error(f"FFmpeg 进程已退出，音频返回码: {self.ffmpeg_process.poll()}, 视频返回码: {self.ffmpeg_video_process.poll()}")
                    break
                
                # 同步读取音频和视频数据
                audio_data = await asyncio.get_event_loop().run_in_executor(
                    None, 
                    self.ffmpeg_process.stdout.read, 
                    audio_frame_size
                )
                
                video_data = await asyncio.get_event_loop().run_in_executor(
                    None, 
                    self.ffmpeg_video_process.stdout.read, 
                    frame_size
                )
                
                # 处理音频数据
                self.ten_env.log_info(f"读取audio帧: {len(audio_data)} 字节")
                if audio_data:
                    await self.audio_queue.put(audio_data)
                
                # 处理视频数据
                if video_data:
                    if len(video_data) == frame_size:
                        self.ten_env.log_info(f"读取video帧: {len(video_data)} 字节")
                        await self.video_queue.put(video_data)
                    else:
                        self.ten_env.log_info(f"警告:期望{frame_size}字节，实际{len(video_data)}字节")
                        continue
                        
        except Exception as e:
            self.ten_env.log_error(f"读取音视频数据时出错: {str(e)}")
        finally:
            self.ten_env.log_info("音视频读取任务结束")

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
            # self.ffmpeg_process.send_signal(signal.SIGINT)
            self.ffmpeg_process.stdin.write(b'q') 
            try:
                self.ffmpeg_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None

        if self.ffmpeg_video_process:
            # self.ffmpeg_process.send_signal(signal.SIGINT)
            self.ffmpeg_video_process.stdin.write(b'q') 
            try:
                self.ffmpeg_video_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_video_process.kill()
            self.ffmpeg_video_process = None
            
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