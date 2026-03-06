import os
import json
import gzip
import asyncio
import logging
import tempfile
import subprocess
import signal
import traceback
from typing import Optional
from fastapi import FastAPI, Request, HTTPException
from datetime import datetime
import httpx
import redis

# 配置
BOT_TOKEN = os.getenv("KOOK_BOT_TOKEN")
VERIFY_TOKEN = os.getenv("KOOK_VERIFY_TOKEN")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Redis 连接
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    USE_REDIS = True
except:
    redis_client = {}
    USE_REDIS = False
    logger.warning("使用内存模式（重启后数据丢失）")

app = FastAPI(title="KOOK Music Bot")

class KookAPI:
    def __init__(self):
        self.token = BOT_TOKEN
        self.base = "https://www.kookapp.cn/api/v3"
        self.headers = {"Authorization": f"Bot {self.token}"}
    
    async def send_msg(self, channel_id: str, content: str):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base}/message/create",
                headers=self.headers,
                json={"target_id": channel_id, "content": content, "type": 1}
            )
    
    async def join_voice(self, channel_id: str):
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base}/voice/join",
                headers=self.headers,
                json={"channel_id": channel_id}
            )
            data = r.json()
            if data.get("code") != 0:
                logger.error(f"加入语音失败: {data}")
                return {}
            return data.get("data", {})
    
    async def leave_voice(self, channel_id: str):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base}/voice/leave",
                headers=self.headers,
                json={"channel_id": channel_id}
            )
    
    async def keep_alive(self, channel_id: str):
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.base}/voice/keep-alive",
                headers=self.headers,
                json={"channel_id": channel_id}
            )

kook = KookAPI()

class Store:
    @staticmethod
    def get(key: str):
        if USE_REDIS:
            return redis_client.get(key)
        return redis_client.get(key)
    
    @staticmethod
    def set(key: str, value: str, ex: int = None):
        if USE_REDIS:
            redis_client.set(key, value, ex=ex)
        else:
            redis_client[key] = value
    
    @staticmethod
    def delete(key: str):
        if USE_REDIS:
            redis_client.delete(key)
        else:
            redis_client.pop(key, None)

async def download_audio(query: str) -> Optional[str]:
    """下载 YouTube 音频"""
    try:
        tmp_dir = tempfile.mkdtemp()
        output_path = f"{tmp_dir}/audio.mp3"
        
        cmd = [
            "yt-dlp",
            f"ytsearch1:{query}",
            "-x", "--audio-format", "mp3",
            "--audio-quality", "128K",
            "-o", output_path,
            "--no-playlist", "--max-filesize", "50M",
            "--user-agent", "Mozilla/5.0"
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        
        if os.path.exists(output_path):
            return output_path
        logger.error(f"下载失败: {stderr.decode()}")
        return None
    except Exception as e:
        logger.error(f"下载异常: {e}")
        return None

async def stream_audio(audio_file: str, guild_id: str):
    """推流到 KOOK"""
    channel_id = Store.get(f"guild:{guild_id}:voice_channel")
    if not channel_id:
        return False
    
    try:
        voice_info = await kook.join_voice(channel_id)
        ip = voice_info.get("ip")
        port = voice_info.get("port")
        ssrc = voice_info.get("ssrc") or voice_info.get("rtcp_port") or 0
        
        if not ip or not port:
            logger.error("获取推流地址失败")
            return False
        
        cmd = [
            "ffmpeg", "-re", "-i", audio_file,
            "-ar", "48000", "-ac", "2", "-c:a", "libopus",
            "-b:a", "128k", "-f", "rtp",
            f"rtp://{ip}:{port}?ssrc={ssrc}"
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        
        Store.set(f"guild:{guild_id}:ffmpeg_pid", str(process.pid))
        await process.communicate()
        Store.delete(f"guild:{guild_id}:ffmpeg_pid")
        
        try:
            os.remove(audio_file)
            os.rmdir(os.path.dirname(audio_file))
        except:
            pass
        return True
    except Exception as e:
        logger.error(f"推流错误: {e}")
        return False

async def play_next(guild_id: str):
    """播放队列下一首"""
    queue_key = f"guild:{guild_id}:queue"
    
    pid = Store.get(f"guild:{guild_id}:ffmpeg_pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except:
            pass
    
    next_song = redis_client.lpop(queue_key) if USE_REDIS else None
    
    if next_song:
        data = json.loads(next_song)
        Store.set(f"guild:{guild_id}:current_song", next_song)
        
        file_path = data.get("file")
        if not file_path or not os.path.exists(file_path):
            file_path = await download_audio(data["title"])
        
        if file_path:
            await stream_audio(file_path, guild_id)
            await play_next(guild_id)
    else:
        Store.delete(f"guild:{guild_id}:current_song")
        text_channel = Store.get(f"guild:{guild_id}:text_channel")
        if text_channel:
            await kook.send_msg(text_channel, "✅ 队列播放完毕")

@app.post("/webhook")
async def webhook(request: Request):
    try:
        raw_body = await request.body()
        logger.info(f"Received body length: {len(raw_body)}")
        
        # 解压（zlib 或 gzip）
        body_text = None
        try:
            body_text = gzip.decompress(raw_body).decode('utf-8')
            logger.info("Gzip decompressed")
        except:
            try:
                import zlib
                body_text = zlib.decompress(raw_body).decode('utf-8')
                logger.info("Zlib decompressed")
            except:
                body_text = raw_body.decode('utf-8', errors='ignore')
        
        logger.info(f"Body: {body_text[:200]}")
        body = json.loads(body_text)
        data = body.get("d", {})
        
        # Challenge 验证（关键：判断 d.type == 255）
        if data.get("type") == 255:
            logger.info(f"Challenge data: {data}")
            if data.get("verify_token") == VERIFY_TOKEN:
                challenge = data.get("challenge")
                logger.info(f"Challenge success: {challenge}")
                return {"challenge": challenge}
            logger.error(f"Token mismatch")
            raise HTTPException(403, "Invalid verify token")
        
        # 处理消息
        if body.get("s") == 0 and data.get("type") == 1:
            await handle_message(data)
        
        return {"code": 0}
        
    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"code": 0}

async def handle_message(data: dict):
    content = data.get("content", "").strip()
    author_id = data.get("author_id")
    guild_id = data.get("guild_id")
    channel_id = data.get("target_id")
    
    # 忽略自己
    if author_id == (BOT_TOKEN.split("/")[1] if "/" in BOT_TOKEN else ""):
        return
    
    if not content.startswith("/"):
        return
    
    parts = content.split(" ", 1)
    cmd = parts[0][1:].lower()
    args = parts[1] if len(parts) > 1 else ""
    
    Store.set(f"guild:{guild_id}:text_channel", channel_id)
    
    if cmd == "help":
        await kook.send_msg(channel_id, 
            "**命令列表**\n/join <ID> - 加入语音\n/leave - 离开\n/play <歌名> - 点歌\n/skip - 切歌\n/say <文字> - 说话"
        )
    
    elif cmd == "join":
        if not args:
            await kook.send_msg(channel_id, "用法: /join 频道ID")
            return
        Store.set(f"guild:{guild_id}:voice_channel", args)
        result = await kook.join_voice(args)
        if result:
            await kook.send_msg(channel_id, "🎤 已加入")
            asyncio.create_task(keep_alive(guild_id, args))
        else:
            await kook.send_msg(channel_id, "❌ 加入失败")
    
    elif cmd == "leave":
        voice_ch = Store.get(f"guild:{guild_id}:voice_channel")
        if voice_ch:
            await kook.leave_voice(voice_ch)
            Store.delete(f"guild:{guild_id}:voice_channel")
            await kook.send_msg(channel_id, "👋 已离开")
    
    elif cmd == "play":
        if not args:
            await kook.send_msg(channel_id, "用法: /play 歌名")
            return
        if not Store.get(f"guild:{guild_id}:voice_channel"):
            await kook.send_msg(channel_id, "请先/join")
            return
        
        await kook.send_msg(channel_id, f"🔍 搜索: {args}")
        file_path = await download_audio(args)
        if not file_path:
            await kook.send_msg(channel_id, "❌ 未找到")
            return
        
        song_data = json.dumps({"title": args, "file": file_path, "user": author_id})
        if USE_REDIS:
            redis_client.rpush(f"guild:{guild_id}:queue", song_data)
        
        current = Store.get(f"guild:{guild_id}:current_song")
        if not current:
            await kook.send_msg(channel_id, f"▶️ 播放: {args}")
            asyncio.create_task(play_next(guild_id))
        else:
            queue_len = redis_client.llen(f"guild:{guild_id}:queue") if USE_REDIS else 1
            await kook.send_msg(channel_id, f"➕ 队列第{queue_len}: {args}")
    
    elif cmd == "skip":
        await play_next(guild_id)
        await kook.send_msg(channel_id, "⏭️ 切歌")
    
    elif cmd == "say":
        if not args:
            return
        if not Store.get(f"guild:{guild_id}:voice_channel"):
            return
        try:
            import edge_tts
            communicate = edge_tts.Communicate(args[:500], "zh-CN-XiaoxiaoNeural")
            tmp_file = f"/tmp/tts_{int(datetime.now().timestamp())}.mp3"
            await communicate.save(tmp_file)
            await stream_audio(tmp_file, guild_id)
        except Exception as e:
            logger.error(f"TTS错误: {e}")

async def keep_alive(guild_id: str, channel_id: str):
    """保活"""
    while True:
        await asyncio.sleep(30)
        if Store.get(f"guild:{guild_id}:voice_channel") == channel_id:
            try:
                await kook.keep_alive(channel_id)
            except:
                break
        else:
            break

@app.get("/")
async def root():
    return {"status": "KOOK Bot Running", "redis": USE_REDIS}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
