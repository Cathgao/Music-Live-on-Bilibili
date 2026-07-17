import asyncio
import json
from bilibili_api import Credential, Danmaku, sync
from bilibili_api.live import LiveDanmaku, LiveRoom, get_self_info
import os
import service.AssMaker
import time, datetime
import urllib
import urllib.request
import requests
import re
from PIL import Image
import io

# --- 配置加载 ---
config = json.load(open('./Config.json', encoding='utf-8'))
_admin_config = config.get('admin', {}).get('ids', ['1762226'])
if isinstance(_admin_config, (str, int)):
    _admin_config = [_admin_config]
admin_ids = {str(admin_id).strip() for admin_id in _admin_config if str(admin_id).strip()}

credential = Credential(sessdata=config["danmu"]["SESSDATA"], bili_jct=config["danmu"]["bili_jct"], buvid3=config["danmu"]["buvid3"], ac_time_value=config["danmu"]["ac_time_value"])
monitor  = LiveDanmaku(int(config['danmu']['roomid']), credential=credential)
sender = LiveRoom(int(config['danmu']['roomid']), credential=credential)
path = config['path']
_tmp_default = "/dev/shm/Music-Live-on-Bilibili-temp/"
try:
    os.makedirs(_tmp_default, exist_ok=True)
    temp_path = _tmp_default
except OSError as _e:
    print(f"警告: 无法创建 {_tmp_default} ({_e}),回退到 {os.path.join(path, 'temp')}")
    temp_path = os.path.join(path, 'temp')
    os.makedirs(temp_path, exist_ok=True)
roomid = config['danmu']['roomid']
_music_api_config = config.get('musicAPI', {})
music_api_url = str(_music_api_config.get('url', 'https://music-api.gdstudio.xyz/api.php')).strip()
music_api_source = str(config.get('musicAPI', {}).get('source', 'netease')).strip() or 'netease'
_valid_music_bitrates = {128, 192, 320, 740, 999}
try:
    music_api_bitrate = int(config.get('musicAPI', {}).get('bitrate', 320))
except (TypeError, ValueError):
    music_api_bitrate = 320
if music_api_bitrate not in _valid_music_bitrates: print(f'警告: musicAPI.bitrate={music_api_bitrate} 无效，回退到 320')
music_api_bitrate = 320
music_api_headers = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux aarch64) Music-Live-on-Bilibili/1.0',
    'Referer': str(_music_api_config.get('referer', 'https://music.gdstudio.xyz/')).strip()
}

skip_flag_file = os.path.join(temp_path, '.skip_current')

AUDIO_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac')

dm_lock = False		 # 弹幕发送锁，用来排队
encode_lock = False	 # 视频渲染锁，用来排队
rp_lock = False      # 点播锁定开关
first_order = False    # 首次点歌标记
self_uid = ''         # 当前登录账号 UID，由直播连接后自动获取
pending_song_choices = {}  # userID -> {'songs': list, 'event': asyncio.Event, 'choice': int}

# --- 图片处理函数 ---
def resize_image_to_1080p(image_bytes):
    """
    将任意大小的图片处理成标准1080P (1920x1080)。
    - 过大的图片：保持长宽比缩小至填满至少一边，然后加黑边
    - 过小的图片：保持长宽比放大至填满至少一边，然后加黑边
    确保所有输出图片都是标准的 1920x1080 分辨率，无裁切。
    
    参数:
        image_bytes: 图片的字节流 (bytes)
    
    返回:
        处理后的图片字节流 (bytes)，如果处理失败返回 None
    """
    try:
        # 从字节流打开图片
        img = Image.open(io.BytesIO(image_bytes))
        
        # 确保是 RGB 格式（处理 RGBA 等格式）
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # 目标分辨率
        target_width = 1920
        target_height = 1080
        
        # 计算缩放比例，保持长宽比，取较大的比例（确保填满至少一边）
        img_ratio = img.width / img.height
        target_ratio = target_width / target_height
        
        if img_ratio > target_ratio:
            # 图片相对宽，按宽度缩放到 1920
            new_width = target_width
            new_height = int(target_width / img_ratio)
        else:
            # 图片相对高，按高度缩放到 1080
            new_height = target_height
            new_width = int(target_height * img_ratio)
        
        # 缩放图片（使用高质量重采样）
        img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # 创建 1920x1080 的黑色背景
        bg = Image.new('RGB', (target_width, target_height), color=(0, 0, 0))
        
        # 计算居中位置
        x = (target_width - new_width) // 2
        y = (target_height - new_height) // 2
        
        # 将缩放后的图片粘贴到背景中心
        bg.paste(img_resized, (x, y))
        
        # 保存处理后的图片到内存中
        output_bytes = io.BytesIO()
        bg.save(output_bytes, format='JPEG', quality=95)
        processed_image = output_bytes.getvalue()
        
        print(f"✅ 图片已在内存中处理完毕，缩放至 {new_width}x{new_height}，黑边填充至 1920x1080，大小: {len(processed_image)} bytes")
        return processed_image
        
    except Exception as e:
        print(f"❌ 图片处理出错: {e}")
        return None

@monitor.on('DANMU_MSG')
async def on_danmaku(event):
    # 收到弹幕
    # print(event)
    if(event["data"]["cmd"]=="DANMU_MSG"):
        commentUser = event['data']['info'][2][1]
        commentText = event['data']['info'][1]
        commentUserID = event['data']['info'][2][0]
    if self_uid and str(commentUserID) == self_uid:
        return
    print(f'{commentUser}({commentUserID})说: {commentText}')
    asyncio.create_task(danmuji.pick_msg(commentUser, commentUserID, commentText))

@monitor.on('SEND_GIFT')
async def on_gift(event):
    # 收到礼物
    print(event)

# --- 同步文件操作/空间检查（保持同步，但只在需要时调用） ---

def del_file(f):
    try:
        print('delete'+path+'/resource/playlist/'+f)
        os.remove(path+'/resource/playlist/'+f)
    except Exception as e:
        print(f'delete error: {e}')

def check_free():
    files = os.listdir(path+'/resource/playlist')
    size = 0
    for f in files:
        size += os.path.getsize(path+'/resource/playlist/'+f)
    files = os.listdir(path+'/resource/music')
    for f in files:
        size += os.path.getsize(path+'/resource/music/'+f)
    
    # 转换为兆字节并比较
    if(size > int(config['freespace'])*1024*1024):
        print(f"space size: {size} bytes, exceeded limit.")
        return True
    else:
        return False

# 检查已使用空间，并在超过时，自动删除缓存的视频
def clean_files():
    is_boom = check_free()
    if is_boom:
         print("Warning: Storage space exceeded. Deletion logic missing or commented out.")
         pass
    return is_boom

# --- 核心异步函数：下载和渲染优化 ---

async def get_download_url(songid, type, user, userID, songname = "nothing"):
    global encode_lock
    
    if clean_files():
        await danmuji.send_dm('Server存储空间已爆炸，请联系up')
        return

    # await danmuji.send_dm(f'正在下载 {type}{songid}')
    print(f'[log] getting url: {type}{songid}')
    filename = str(int(time.mktime(datetime.datetime.now().timetuple())))

    try:
        if type == 'id':
            def sync_download_id():
                song_data = songid if isinstance(songid, dict) else {
                    'id': songid,
                    'name': songname,
                    'source': music_api_source
                }
                track_id = str(song_data.get('id', '')).strip()
                source = str(song_data.get('source') or music_api_source).strip()
                if not track_id:
                    print('❌ 搜索结果缺少歌曲 ID')
                    return '', '', '', '', ''

                def get_api_json(params, timeout=10):
                    response = requests.get(
                        music_api_url,
                        params=params,
                        headers=music_api_headers,
                        timeout=timeout
                    )
                    try:
                        response.raise_for_status()
                        data = response.json()
                    finally:
                        response.close()
                    if not isinstance(data, dict):
                        raise ValueError(f'音乐 API 返回格式异常: {params["types"]}')
                    return data

                url_data = get_api_json({
                    'types': 'url',
                    'source': source,
                    'id': track_id,
                    'br': music_api_bitrate
                })
                download_url = str(url_data.get('url') or '').strip()
                if not download_url or int(url_data.get('br', -1) or -1) <= 0:
                    print(f'❌ 歌曲无可用下载地址: {source}/{track_id}')
                    return '', '', '', '', ''

                lyric = ''
                tlyric = ''
                lyric_id = str(song_data.get('lyric_id') or track_id).strip()
                try:
                    lyric_data = get_api_json({
                        'types': 'lyric',
                        'source': source,
                        'id': lyric_id
                    })
                    lyric = lyric_data.get('lyric') or ''
                    tlyric = lyric_data.get('tlyric') or ''
                except Exception as e:
                    print(f'⚠️ 歌词获取失败，继续下载歌曲: {e}')

                pic_id = str(song_data.get('pic_id') or '').strip()
                if pic_id:
                    try:
                        pic_data = get_api_json({
                            'types': 'pic',
                            'source': source,
                            'id': pic_id,
                            'size': 500
                        })
                        pic_url = str(pic_data.get('url') or '').strip()
                        if pic_url:
                            pic_response = requests.get(
                                pic_url,
                                headers=music_api_headers,
                                timeout=15
                            )
                            try:
                                pic_response.raise_for_status()
                                processed_pic_bytes = resize_image_to_1080p(pic_response.content)
                            finally:
                                pic_response.close()
                            if processed_pic_bytes:
                                final_pic_path = f'{path}/resource/playlist/{filename}.jpg'
                                with open(final_pic_path, 'wb') as f_pic:
                                    f_pic.write(processed_pic_bytes)
                                print(f'✅ 封面图片成功保存到: {final_pic_path}')
                        else:
                            print(f'⚠️ 歌曲没有可用封面: {source}/{track_id}')
                    except Exception as e:
                        print(f'⚠️ 封面获取失败，继续下载歌曲: {e}')

                response = requests.get(
                    download_url,
                    stream=True,
                    headers=music_api_headers,
                    timeout=(10, 60)
                )
                audio_path = ''
                try:
                    response.raise_for_status()
                    extension_name = os.path.splitext(
                        os.path.basename(urllib.parse.urlparse(download_url).path)
                    )[1].lower()
                    content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
                    content_type_extensions = {
                        'audio/mpeg': '.mp3',
                        'audio/mp4': '.m4a',
                        'audio/x-m4a': '.m4a',
                        'audio/flac': '.flac',
                        'audio/x-flac': '.flac',
                        'audio/ogg': '.ogg',
                        'audio/aac': '.aac',
                        'audio/wav': '.wav',
                        'audio/x-wav': '.wav'
                    }
                    if extension_name not in AUDIO_EXTENSIONS:
                        extension_name = content_type_extensions.get(content_type, '.mp3')
                    audio_path = f'{path}/resource/playlist/{filename}{extension_name}'
                    with open(audio_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                except Exception:
                    if audio_path and os.path.isfile(audio_path):
                        os.remove(audio_path)
                    raise
                finally:
                    response.close()

                artists = song_data.get('artist') or []
                if isinstance(artists, str):
                    artists = [artists]
                artist_text = '、'.join(str(artist) for artist in artists if artist)
                song_temp = str(song_data.get('name') or songname or track_id)
                print(f'✅ 文件成功下载并保存到: {audio_path}')
                return lyric, tlyric, song_temp, artist_text, source

            lyric, tlyric, song_temp, artist_text, source = await asyncio.to_thread(sync_download_id)
            if not song_temp:
                await danmuji.send_dm('点歌失败：歌曲没有可用下载地址')
                return

            track_id = songid.get('id') if isinstance(songid, dict) else songid
            song = f'歌名：{song_temp}'
            if artist_text:
                song += f' / 歌手：{artist_text}'
            service.AssMaker.make_ass(
                filename,
                f'当前音乐源：{source}，id：{track_id}\\N{song}\\N点播人：{user}',
                path,
                lyric,
                tlyric
            )
            service.AssMaker.make_info(
                filename,
                f'来源：{source}，id：{track_id}，{song}，点播人：{user}',
                userID,
                path
            )
            # 第一首点播歌曲直接切
            global first_order
            if(first_order):
                first_order = False
                try:
                    with open(skip_flag_file, 'w') as f:
                        f.write('skip')
                    print(f'[log] 发送切歌信号')
                    await danmuji.send_dm(f'歌曲 {song_temp} 下载完成，准备播放')
                except Exception as e:
                    print(f'[log] 切歌信号发送失败: {e}')
            else:
                await danmuji.send_dm(f'歌曲 {song_temp} 下载完成，已加入播放队列')
                print(f'[log] 已添加排队项目：{source}/{track_id}')

        elif type == 'mv':
            def sync_process_mv():
                # 1. 获取 MV URL
                params = urllib.parse.urlencode({type: songid})
                f = urllib.request.urlopen(download_api_url + "?%s" % params, timeout=5)
                url = f.read().decode('utf-8')
                
                # 2. 下载 MV
                urllib.request.urlretrieve(url, f'{path}/resource/playlist/{filename}.mp4')
                
                return url # 返回 URL 供日志使用

            url = await asyncio.to_thread(sync_process_mv) # 在线程池中执行
            
            print(f'[log] 获取{type}{songid}网址：{url}')
            print(f'[log] {type}{songid} 下载完成')
            
            # 生成字幕信息（非阻塞）
            info_text = f"使用API：GD音乐台(music.gdstudio.xyz)\\N" + \
                        f"当前MV网易云id：{songid}\\N" + \
                        (f"MV点播关键词：{songname}\\N" if songname != "nothing" else "") + \
                        f"点播人：{user}"
            service.AssMaker.make_ass(f'{filename}ok', info_text, path)
            service.AssMaker.make_info(f'{filename}ok', info_text.replace('\\N', ','), path)
            
            await danmuji.send_dm(f'{type}{songid} 下载完成，等待渲染')
            
            # 渲染锁（等待渲染完成）
            while encode_lock:
                await asyncio.sleep(1)
            encode_lock = True
            
            await danmuji.send_dm(f'{type}{songid} 正在渲染')
            print(f'[log] {type}{songid} 正在渲染')
            
            def sync_render_mv():
                cmd = f'ffmpeg -threads 1 -i "{path}/resource/playlist/{filename}.mp4" -aspect 16:9 -vf "scale=1280:720, ass={path}/resource/playlist/{filename}ok.ass" -c:v libx264 -strict -2 -preset ultrafast -maxrate {config["rtmp"]["bitrate"]}k -tune fastdecode -acodec aac -b:a 192k "{path}/resource/playlist/{filename}rendering.flv"'
                os.system(cmd)
            
            await asyncio.to_thread(sync_render_mv) # 在线程池中执行 FFMPEG
            
            encode_lock = False
            del_file(f'{filename}.mp4')
            os.rename(f'{path}/resource/playlist/{filename}rendering.flv', f'{path}/resource/playlist/{filename}ok.flv')
            
            await danmuji.send_dm(f'{type}{songid} 渲染完毕，已加入播放队列')
            print(f'[log] {type}{songid} 渲染完毕，已加入播放队列')

    except Exception as e:
        await danmuji.send_dm('出错了：请检查命令或重试')
        print(f'[log] 下载文件出错：{type}{songid}')
        print(e)
        for suffix in ('.mp3', '.flac', '.m4a', '.wav', '.ogg', '.aac', '.jpg', '.ass', '.info', '.mp4', '.flv'):
            del_file(f'{filename}{suffix}')

# 下载歌单
async def playlist_download(id,user):
    def sync_get_playlist():
        params = urllib.parse.urlencode({'playlist': str(id)})
        f = urllib.request.urlopen(download_api_url + "?%s" % params, timeout=3)
        return json.loads(f.read().decode('utf-8'))
    
    try:
        playlist = await asyncio.to_thread(sync_get_playlist) # 在线程池中获取
        await danmuji.send_dm(f'正在下载歌单：{playlist["playlist"]["name"]}，共{len(playlist["playlist"]["tracks"])}首')
    except Exception as e:
        print(f'shit(playlist): {e}')
        await danmuji.send_dm('出错了：请检查命令或重试')
        return

    # 遍历歌单歌曲并启动下载任务
    for song in playlist['playlist']['tracks']:
        print(f'name:{song["name"]} id:{song["id"]}')
        asyncio.create_task(song['id'], 'id', user, song['name'])

# 搜索歌曲并下载
async def search_song(song_name,user,userID):
    print(f'[log] searching song: {song_name}')

    def sync_search():
        response = requests.get(
            music_api_url,
            params={
                'types': 'search',
                'source': music_api_source,
                'name': song_name,
                'count': 3,
                'pages': 1
            },
            headers=music_api_headers,
            timeout=10
        )
        try:
            response.raise_for_status()
            search_result = response.json()
        finally:
            response.close()
        if not isinstance(search_result, list):
            raise ValueError('音乐搜索 API 返回格式异常')
        return search_result[:3]

    try:
        search_result = await asyncio.to_thread(sync_search)
        if not search_result:
            await danmuji.send_dm(f'点歌失败，找不到该歌曲或没有版权：{song_name}')
            return

        valid_results = [song for song in search_result
                         if isinstance(song, dict) and song.get('id')]
        if not valid_results:
            raise ValueError('音乐搜索结果缺少歌曲 ID')

        if len(valid_results) > 1:
            choice_event = asyncio.Event()
            choice_session = {
                'songs': valid_results,
                'event': choice_event,
                'choice': None
            }
            pending_song_choices[str(userID)] = choice_session

            await danmuji.send_dm('你要点哪首？回复1-3选择 30秒后自动选择第1首')
            await asyncio.sleep(2)
            for index, song in enumerate(valid_results, 1):
                artists = song.get('artist') or []
                if isinstance(artists, str):
                    artists = [artists]
                artist_text = '、'.join(str(artist) for artist in artists if artist) or '未知歌手'
                song_text = f'{song.get("name") or "未知歌曲"}-{artist_text}'
                message = f'{index}.{song_text}'[:40]
                await danmuji.send_dm(message)
                if index < len(valid_results):
                    await asyncio.sleep(1)

            try:
                await asyncio.wait_for(choice_event.wait(), timeout=30)
                choice = choice_session['choice']
                print(f'[log] 用户 {userID} 选择了第 {choice} 首歌曲')
            except asyncio.TimeoutError:
                choice = 1
                print(f'[log] 用户 {userID} 30秒内未选择，自动选择第1首歌曲')
            finally:
                pending_song_choices.pop(str(userID), None)
            song_data = valid_results[choice - 1]
        else:
            song_data = valid_results[0]

        await get_download_url(song_data, 'id', user, userID, song_name)

    except Exception as e:
        pending_song_choices.pop(str(userID), None)
        await danmuji.send_dm(f'搜索歌曲 {song_name} 时发生错误')
        print(f'[error] Search failed: {e}')


class bilibiliClient():
    async def startup(self):
        global self_uid
        # 先获取登录账号 UID，再开始接收弹幕
        try:
            self_uid = str(getattr(credential, 'dedeuserid', '') or '')
            if not self_uid:
                self_info = await get_self_info(credential)
                self_uid = str(
                    self_info.get('info', {}).get('uid')
                    or self_info.get('data', {}).get('mid')
                    or self_info.get('uid', '')
                    or ''
                )
            if self_uid:
                print(f'[log] 当前登录账号 UID: {self_uid}，将过滤自己的弹幕')
            else:
                print('[warn] 无法获取当前登录账号 UID，将不主动过滤弹幕')
        except Exception as e:
            self_uid = ''
            print(f'[warn] 获取当前登录账号 UID 失败，将不主动过滤弹幕: {e}')

        # 连接直播间并保持连接，直到外部中断
        await monitor.connect()
        # 以下为测试代码
        # commentUser = "TEST3"
        # commentText = "点歌 snooze"
        # commentUserID = "14341"
        # await danmuji.pick_msg(commentUser, commentUserID, commentText)
        
    async def send_dm(self, Text):
        print(f'[DM_SENT] {Text}')
        # pass # 保持异步兼容
        await sender.send_danmaku(Danmaku(Text))

    async def pick_msg(self, User, UserID, Text):
        # 优先处理候选歌曲选择，只接受对应点歌者的合法选择
        pending_choice = pending_song_choices.get(str(UserID))
        if pending_choice is not None:
            match = re.fullmatch(r'(?:选择?|选)?\s*([0-9]+)\s*', str(Text))
            if match:
                choice = int(match.group(1))
                if 1 <= choice <= len(pending_choice['songs']):
                    pending_choice['choice'] = choice
                    pending_choice['event'].set()
                    return
            print(f'[log] 忽略用户 {UserID} 的非法歌曲选择: {Text}')
            return

        # 其他用户的数字或选择消息不能影响待选择会话
        if re.fullmatch(r'(?:选择?|选)?\s*[0-9]+\s*', str(Text)):
            return

          # 获取第一个音频文件的信息
        def sync_get_current_song_info():
                files = os.listdir(f'{path}/resource/playlist')
                files.sort()  # 按文件名（下载时间）排序
                current_audio_file = None
                for f in files:
                    # 找到第一个符合音频扩展名且不是正在下载的临时文件的文件
                    if f.endswith(AUDIO_EXTENSIONS) and (f.find('.download') == -1):
                        current_audio_file = f
                        break
                if current_audio_file:
                    try:
                        base_name, _ = os.path.splitext(current_audio_file)
                        info_file_path = f'{path}/resource/playlist/{base_name}.info'
                        with open(info_file_path, 'r', encoding='utf-8') as info_file:
                            # 只获取第二行
                            info_file.readline()
                            requester_id = info_file.readline().strip()
                            return requester_id
                    except FileNotFoundError:
                        print(f"⚠️ 找不到对应的 .info 文件: {info_file_path}")
                        return ""
                    except Exception as e:
                        print(f"❌ 读取 .info 文件出错: {e}")
                        return ""
                else:
                    return "" # 播放列表为空

        
        global encode_lock
        global rp_lock
        # 管理员命令
        if str(UserID) in admin_ids:
            print(f'[log] 管理员 {User}({UserID}) 发送: {Text}')
            if Text == '锁定':
                rp_lock = True
                await self.send_dm('已锁定点播功能，不响应任何弹幕')
                return
            elif Text == '解锁':
                rp_lock = False
                await self.send_dm('已解锁点播功能，恢复响应弹幕')
                return
            elif Text == '清空列表':
                if encode_lock:
                    await self.send_dm('有渲染任务，无法清空')
                    return
                # 将阻塞的 os.listdir 和 del_file 放在线程中运行
                def sync_clean():
                    for i in os.listdir(f'{path}/resource/playlist'):
                        del_file(i)
                
                await asyncio.to_thread(sync_clean)
                await self.send_dm('已经清空列表~')
                return
        # 点播功能检查
        if rp_lock:
            return # 如果锁定，则不响应普通弹幕
        
        #查找关键词
        keyword = '点歌'
        start_index = Text.find(keyword)
        # 检查是否找到了 "点歌" 关键词
        if start_index != -1:
            extracted_content = Text[start_index + len(keyword):].strip()
            if extracted_content:
                # 检查当前有没有点播的歌曲
                is_playlist_empty = True  # 假设播放列表为空
                for root, dirs, files in os.walk(f'{path}/resource/playlist'):
                    for filename in files:
                        file_extension = os.path.splitext(filename)[1].lower()
                        if file_extension in AUDIO_EXTENSIONS:
                            # 找到一个音频文件，说明播放列表不为空
                            print(f"✅ 播放列表中找到音频文件: {os.path.join(root, filename)}")
                            is_playlist_empty = False
                            # 找到后立即退出两层循环，停止文件搜索
                            break
                    if not is_playlist_empty:
                        break # 退出 os.walk 的最外层循环
                # 设置首次点歌标记
                global first_order
                first_order = is_playlist_empty
                # 异步搜索并下载
                await search_song(extracted_content, User, UserID)
            else:
                await self.send_dm('点歌格式：点歌 [歌曲名]')

        if((Text == '点播列表') or (Text == '歌曲列表')):
            await danmuji.send_dm('已收到'+User+'的指令，正在查询')
            files = os.listdir(path+'/resource/playlist')   #获取目录下所有文件
            files.sort()    #按文件名（下载时间）排序
            songs_count = 0 #项目数量
            all_the_text = ""
            for f in files:
                if((f.endswith(AUDIO_EXTENSIONS)) and (f.find('.download') == -1)): 
                    try:
                        base_name, _ = os.path.splitext(f) 
                        info_file = open(f'{path}/resource/playlist/{base_name}.info', 'r' ,encoding='utf-8') 
                        all_the_text = info_file.readline().strip()
                        all_the_text = info_file.readline().strip()
                        info_file.close()
                    except Exception as e:
                        print(e)
                    if(songs_count < 10):
                        await asyncio.sleep(2)
                        await danmuji.send_dm(all_the_text)
                    songs_count += 1
            if(songs_count == 0):
                await danmuji.send_dm('当前点播列表为空')
                return
            if(songs_count <= 10):
                await asyncio.sleep(2)
                await danmuji.send_dm('点播列表展示完毕，一共'+str(songs_count)+'个')
            else:
                await danmuji.send_dm('点播列表前十个展示完毕，一共'+str(songs_count)+'个')
        
        if(Text == '切歌' or Text == '下一首'):
            current_song_id = sync_get_current_song_info()
            if str(UserID) in admin_ids or str(current_song_id) == str(UserID) or current_song_id == "":
                try:
                    with open(skip_flag_file, 'w') as f:
                        f.write('skip')
                    await self.send_dm('已发送切歌信号，请稍后')
                    print(f'[log] 收到切歌命令，已发送切歌信号')
                except Exception as e:
                    await self.send_dm('切歌失败')
                    print(f'[log] 切歌信号发送失败: {e}')
            else:
                await self.send_dm('不是你点的歌')

if __name__ == '__main__':
    should_restart = True
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        danmuji = bilibiliClient()
        # 1. 运行 startup 任务，等待连接成功
        print('正在连接弹幕服务器...')
        loop.run_until_complete(danmuji.startup())
        
        print('连接弹幕服务器成功，事件循环开始持续运行...')
        # 2. 使用 run_forever() 让事件循环持续监听弹幕
        loop.run_forever()
        
    except KeyboardInterrupt:
        should_restart = False
        print('程序被用户中断 (Ctrl+C). 正在安全退出...')
    except Exception as e:
        print(f'[error] 脚本发生错误: {e}')
        
    finally:
        # 3. 清理工作
        print('开始清理任务并关闭连接...')
        try:
            if not loop.is_closed():
                loop.run_until_complete(monitor.disconnect())
        except Exception as e:
            print(f'[warn] 关闭弹幕连接失败: {e}')
        
        # 取消所有仍在运行的异步任务
        pending = asyncio.all_tasks(loop)
        if pending:
            print(f'正在取消 {len(pending)} 个任务...')
            for task in pending:
                task.cancel()
            
            # 等待所有任务真正结束
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            
        # 停止并关闭循环
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
             loop.close()
        
        # 4. 仅在非用户主动中断时自动重启
        if should_restart:
            print('尝试自动重启脚本...')
            os.system(f"python3 {config['path']}/Danmu.py")