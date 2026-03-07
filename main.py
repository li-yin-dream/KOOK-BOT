import os
import json
import gzip
import asyncio
import logging
import tempfile
import subprocess
import signal
import traceback
import zlib
from typing import Optional
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx
import redis

# --- 配置 ---
BOT_TOKEN = os.getenv("KOOK_BOT_TOKEN")
VERIFY_TOKEN = os.getenv("KOOK_VERIFY_TOKEN")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
PORT = int(os.getenv("PORT", "8000"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("main")

# --- Redis 连接 ---
redis_client = None
USE_REDIS = False

try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    USE_REDIS = True
    logger.info("✅ Redis 连接成功")
except Exception as e:
    logger.warning(f"⚠️ Redis 连接失败 ({e})，使用内存模式（重启后数据丢失）")
    redis_client = {} #  fallback 为字典

app = FastAPI(title="KOOK Music Bot")

# --- KOOK API 类 ---
class KookAPI:
    def __init__(self):
        self.token = BOT_TOKEN
        self.base = "https://www.kookapp.cn/api/v3"
        self.headers = {"Authorization": f"Bot {self.token}"}
    
    async def send_msg(self, channel_id: str, content: str):
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self.base}/message/create",
                    headers=self.headers,
                    json={"target_id": channel_id, "content": content, "type": 1},
                    timeout=10.0
                )
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
    
    async def join_voice(self, channel_id: str):
        async with httpx.AsyncClient() as client:
            try:
                r = await client.post(
                    f"{self.base}/voice/join",
                    headers=self.headers,
                    json={"channel_id": channel_id},
                    timeout=10.0
                )
                data = r.json()
                if data.get("code") != 0:
                    logger.error(f"加入语音失败: {data}")
                    return {}
                return data.get("data", {})
            except Exception as e:
                logger.error(f"加入语音异常: {e}")
                return {}
    
    async def leave_voice(self, channel_id: str):
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self.base}/voice/leave",
                    headers=self.headers,
                    json={"channel_id": channel_id},
                    timeout=10.0
                )
            except Exception as e:
                logger.error(f"离开语音异常: {e}")
    
    async def keep_alive(self, channel_id: str):
        async with httpx.AsyncClient() as client:
            try:
                await client.post(
                    f"{self.base}/voice/keep-alive",
                    headers=self.headers,
                    json={"channel_id": channel_id},
                    timeout=10.0
                )
            except Exception as e:
                logger.error(f"保活失败: {e}")

kook = KookAPI()

# --- 存储辅助类 (兼容 Redis 和 内存) ---
class Store:
    @staticmethod
    def get(key: str):
        if USE_REDIS:
            return redis_client.get(key)
        return redis_client.get(key) # 字典模式
    
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

    @staticmethod
    def lpush(key: str, value: str):
        if USE_REDIS:
            redis_client.lpush(key, value)
        else:
            if key not in redis_client:
                redis_client[key] = []
            redis_client[key].insert(0, value)

    @staticmethod
    def rpush(key: str, value: str):
        if USE_REDIS:
            redis_client.rpush(key, value)
        else:
            if key not in redis_client:
                redis_client[key] = []
            redis_client[key].append(value)

    @staticmethod
    def lpop(key: str):
        if USE_REDIS:
            return redis_client.lpop(key)
        else:
            if key in redis_client and len(redis_client[key]) > 0:
                return redis_client[key].pop(0)
            return None
            
    @staticmethod
    def llen(key: str):
        if USE_REDIS:
            return redis_client.llen(key)
        else:
            return len(redis_client.get(key, []))

# --- 核心功能函数 ---

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
    
    # 停止当前播放
    pid = Store.get(f"guild:{guild_id}:ffmpeg_pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except:
            pass
        # 等待一小会儿让进程清理
        await asyncio.sleep(0.5)
    
    next_song = Store.lpop(queue_key)
    
    if next_song:
        # 如果是内存模式，next_song 可能是 dict，需要转回 json 字符串处理，或者统一存字符串
        if not USE_REDIS and isinstance(next_song, dict):
             # 内存模式下我们存的是对象还是字符串？上面 rpush 存的是 json.dumps 后的字符串
             # 这里 lpop 取出的是字符串，所以不用转
             pass 
        
        try:
            data = json.loads(next_song) if isinstance(next_song, str) else next_song
        except:
            data = next_song # 兼容处理

        Store.set(f"guild:{guild_id}:current_song", next_song if isinstance(next_song, str) else json.dumps(next_song))
        
        file_path = data.get("file")
        if not file_path or not os.path.exists(file_path):
            logger.info(f"文件不存在，重新下载: {data.get('title')}")
            file_path = await download_audio(data["title"])
        
        if file_path:
            await stream_audio(file_path, guild_id)
            # 递归播放下一首
            await play_next(guild_id)
    else:
        Store.delete(f"guild:{guild_id}:current_song")
        text_channel = Store.get(f"guild:{guild_id}:text_channel")
        if text_channel:
            await kook.send_msg(text_channel, "✅ 队列播放完毕")

# --- Webhook 路由 (最终修复版) ---

@app.api_route("/webhook", methods=["GET", "POST", "HEAD"])
async def webhook(request: Request):
    from fastapi.responses import Response, JSONResponse

    # HEAD 请求：健康检查
    if request.method == "HEAD":
        return Response(status_code=200)

    # GET 请求：用于浏览器调试
    if request.method == "GET":
        challenge = request.query_params.get("challenge")
        if challenge:
            logger.info(f"✅ [GET] Challenge received: {challenge}")
            return Response(content=challenge, media_type="text/plain")
        raise HTTPException(status_code=400, detail="Missing challenge parameter")

    # POST 请求：KOOK 官方验证 & 消息
    if request.method == "POST":
        try:
            raw_body = await request.body()
            logger.info(f"📦 Received body length: {len(raw_body)}")
            
            if not raw_body:
                logger.warning("Empty body received")
                return {"code": 0}

            # 解压
            try:
                body_text = zlib.decompress(raw_body).decode('utf-8')
                logger.info("✅ Zlib decompressed successfully")
            except Exception as e:
                logger.warning(f"Zlib failed: {e}, trying raw deflate")
                try:
                    body_text = zlib.decompress(raw_body, wbits=-15).decode('utf-8')
                except Exception as e2:
                    logger.error(f"Decompression failed: {e2}")
                    body_text = raw_body.decode('utf-8', errors='ignore')

            logger.info(f"📄 Body text: {body_text[:200]}...")  # 打印前200字符
            
            body = json.loads(body_text)
            data = body.get("d", {})
            logger.info(f"📊 Parsed data: {data}")

            # 处理 Challenge (type 255)
            if data.get("type") == 255:
                challenge_code = data.get("challenge")
                verify_token = data.get("verify_token")
                
                logger.info(f"🎯 Challenge code: {challenge_code}")
                logger.info(f"🔑 Verify token received: {verify_token}")
                logger.info(f"🔒 Expected token: {VERIFY_TOKEN}")

                if VERIFY_TOKEN and verify_token != VERIFY_TOKEN:
                    logger.error("❌ Token mismatch!")
                    raise HTTPException(403, "Invalid verify token")

                if not challenge_code:
                    logger.error("❌ Missing challenge code!")
                    raise HTTPException(400, "Missing challenge")

                # ✅ 返回 JSON
                response_data = {"challenge": challenge_code}
                logger.info(f"✅ Returning: {response_data}")
                return JSONResponse(content=response_data)

            # 处理正常消息
            if body.get("s") == 0 and data.get("type") == 1:
                asyncio.create_task(handle_message(data))

            return {"code": 0}

        except Exception as e:
            logger.error(f"❌ Webhook error: {e}")
            logger.error(traceback.format_exc())
            return {"code": 0}

    raise HTTPException(status_code=405, detail="Method not allowed")
    
# --- 消息处理逻辑 ---

async def handle_message(data: dict):
    content = data.get("content", "").strip()
    author_id = data.get("author_id")
    guild_id = data.get("guild_id")
    channel_id = data.get("target_id")
    
    # 简单防循环
    if not BOT_TOKEN:
        return
    # 注意：author_id 在私信和群聊中格式可能不同，这里做简单判断
    # 如果 bot_token 格式不对，这步跳过也没事
    try:
        bot_id_part = BOT_TOKEN.split(".")[0] if "." in BOT_TOKEN else ""
        if author_id == bot_id_part:
            return
    except:
        pass
    
    if not content.startswith("/"):
        return
    
    parts = content.split(" ", 1)
    cmd = parts[0][1:].lower()
    args = parts[1] if len(parts) > 1 else ""
    
    Store.set(f"guild:{guild_id}:text_channel", channel_id)
    
    if cmd == "help":
        await kook.send_msg(channel_id, 
            "**命令列表**\n/join <频道ID> - 加入语音\n/leave - 离开\n/play <歌名> - 点歌\n/skip - 切歌\n/say <文字> - 说话"
        )
    
    elif cmd == "join":
        if not args:
            await kook.send_msg(channel_id, "用法: /join 频道ID")
            return
        Store.set(f"guild:{guild_id}:voice_channel", args)
        result = await kook.join_voice(args)
        if result and result.get("ip"):
            await kook.send_msg(channel_id, "🎤 已加入语音频道")
            # 启动保活任务
            asyncio.create_task(keep_alive_loop(guild_id, args))
        else:
            await kook.send_msg(channel_id, "❌ 加入失败，请检查频道ID或权限")
    
    elif cmd == "leave":
        voice_ch = Store.get(f"guild:{guild_id}:voice_channel")
        if voice_ch:
            await kook.leave_voice(voice_ch)
            Store.delete(f"guild:{guild_id}:voice_channel")
            # 停止当前播放
            pid = Store.get(f"guild:{guild_id}:ffmpeg_pid")
            if pid:
                try: os.kill(int(pid), signal.SIGTERM)
                except: pass
            await kook.send_msg(channel_id, "👋 已离开语音频道")
    
    elif cmd == "play":
        if not args:
            await kook.send_msg(channel_id, "用法: /play 歌名 或 URL")
            return
        if not Store.get(f"guild:{guild_id}:voice_channel"):
            await kook.send_msg(channel_id, "请先使用 /join 加入语音频道")
            return
        
        await kook.send_msg(channel_id, f"🔍 正在搜索: {args} ...")
        
        # 异步下载，不阻塞
        file_path = await download_audio(args)
        if not file_path:
            await kook.send_msg(channel_id, "❌ 未找到歌曲或下载失败")
            return
        
        song_data = json.dumps({"title": args, "file": file_path, "user": author_id})
        Store.rpush(f"guild:{guild_id}:queue", song_data)
        
        current = Store.get(f"guild:{guild_id}:current_song")
        if not current:
            await kook.send_msg(channel_id, f"▶️ 开始播放: {args}")
            asyncio.create_task(play_next(guild_id))
        else:
            queue_len = Store.llen(f"guild:{guild_id}:queue")
            await kook.send_msg(channel_id, f"➕ 已加入队列 (第 {queue_len} 首): {args}")
    
    elif cmd == "skip":
        await kook.send_msg(channel_id, "⏭️ 正在切歌...")
        await play_next(guild_id)
    
    elif cmd == "say":
        if not args:
            return
        if not Store.get(f"guild:{guild_id}:voice_channel"):
            return
        
        try:
            # 动态导入 edge_tts，防止未安装时报错
            import edge_tts
            communicate = edge_tts.Communicate(args[:500], "zh-CN-XiaoxiaoNeural")
            tmp_file = f"/tmp/tts_{int(datetime.now().timestamp())}.mp3"
            await communicate.save(tmp_file)
            
            await kook.send_msg(channel_id, f"🔊 正在朗读: {args[:20]}...")
            # 临时插入队列头部或直接播放？这里选择直接播放逻辑比较复杂，
            # 简单起见，我们把它当作一首歌加入队列头部
            song_data = json.dumps({"title": f"TTS: {args[:20]}", "file": tmp_file, "user": author_id})
            
            # 插入到队列最前面 (lpush)
            Store.lpush(f"guild:{guild_id}:queue", song_data)
            
            # 如果当前没在播，立即触发播放
            if not Store.get(f"guild:{guild_id}:current_song"):
                asyncio.create_task(play_next(guild_id))
                
        except ImportError:
            await kook.send_msg(channel_id, "❌ 未安装 edge-tts 库，无法使用朗读功能")
        except Exception as e:
            logger.error(f"TTS错误: {e}")
            await kook.send_msg(channel_id, "❌ 朗读失败")

async def keep_alive_loop(guild_id: str, channel_id: str):
    """保活循环"""
    while True:
        await asyncio.sleep(25) # 每 25 秒保活一次 (KOOK 要求 30s 内)
        if Store.get(f"guild:{guild_id}:voice_channel") == channel_id:
            try:
                await kook.keep_alive(channel_id)
            except Exception as e:
                logger.error(f"保活失败: {e}")
                break
        else:
            break

# 将 @app.get("/") 改为 @app.head("/", include_in_schema=False) 和 @app.get("/")
# 或者更简单地，使用 @app.api_route 来同时支持多种方法

@app.api_route("/", methods=["GET", "HEAD"])
async def root(request: Request):
    # 如果是 HEAD 请求，我们只需要返回一个空的 200 OK 响应即可
    if request.method == "HEAD":
        from fastapi.responses import Response
        return Response(status_code=200)
    
    # 如果是 GET 请求，返回正常的 JSON 信息
    return {"status": "KOOK Bot Running", "redis": USE_REDIS, "time": datetime.now().isoformat()}
    
if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting server on port {PORT}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
