import os
import aiohttp
import asyncio
import time
from collections import defaultdict
import re
from datetime import datetime, timedelta


def get_dynamic_keywords():
    fixed_keywords = ["免费提供"]
    return fixed_keywords

def contains_date(text):
    date_pattern = r"\d{4}-\d{2}-\d{2}"
    return re.search(date_pattern, text) is not None


CONFIG = {
    "timeout": 10,
    "max_parallel": 30,
    "output_file": "best_sorted.m3u",
    "iptv_directory": "IPTV"
}


def load_cctv_channels(file_path=".github/workflows/IPTV/CCTV.txt"):
    cctv_channels = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                if line:
                    cctv_channels.add(line)
    except FileNotFoundError:
        print(f"Error: The file {file_path} was not found.")
    return cctv_channels


def load_province_channels(files):
    province_channels = defaultdict(set)
    for file_path in files:
        province_name = os.path.basename(file_path).replace(".txt", "")
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                for line in file:
                    line = line.strip()
                    if line:
                        province_channels[province_name].add(line)
        except FileNotFoundError:
            print(f"Error: The file {file_path} was not found.")
    return province_channels


def normalize_cctv_name(channel_name):
    return re.sub(r'CCTV[-]?(\d+)', r'CCTV\1', channel_name)


def extract_urls_from_txt(content):
    urls = []
    for line in content.splitlines():
        line = line.strip()
        if line and ',' in line:
            parts = line.split(',', 1)
            urls.append(parts)
    return urls


def extract_urls_from_m3u(content):
    urls = []
    lines = content.splitlines()
    channel = "Unknown"
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            parts = line.split(',', 1)
            channel = parts[1] if len(parts) > 1 else "Unknown"
        elif line.startswith(('http://', 'https://')):
            urls.append((channel, line))
    return urls


async def check_m3u8_is_live(session, url, timeout):
    """
    检查 m3u8 是否为直播流（而不是点播/回放）
    直播流：没有 #EXT-X-ENDLIST 标签
    点播流：有 #EXT-X-ENDLIST 标签，或总时长很短
    返回 True 表示是直播，False 表示是点播
    """
    try:
        async with session.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 VLC/3.0"}) as resp:
            if resp.status != 200:
                return False
            text = await asyncio.wait_for(resp.text(errors="ignore"), timeout=8)

            # 有 #EXT-X-ENDLIST 说明是点播，直接丢弃
            if "#EXT-X-ENDLIST" in text:
                return False

            # 计算总时长：累加所有 #EXTINF 的秒数
            durations = re.findall(r'#EXTINF:([\d.]+)', text)
            if durations:
                total_seconds = sum(float(d) for d in durations)
                # 总时长小于 300 秒（5分钟）视为点播
                if total_seconds < 300:
                    return False

            # 没有 #EXT-X-ENDLIST 且时长足够 → 直播
            return True

    except Exception:
        return False


async def test_stream(url):
    """
    测试 IPTV 链接：
    1. HTTP 状态必须是 200
    2. Content-Type 必须是视频/音频流类型
    3. 如果是 m3u8，额外检测是否为直播流（排除点播/回放）
    """
    live_types = [
        "video/",
        "audio/",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/octet-stream",
        "video/mp2t",
    ]
    not_live_types = [
        "text/html",
        "text/xml",
        "application/json",
        "image/",
        "text/plain",
    ]

    try:
        timeout = aiohttp.ClientTimeout(total=CONFIG["timeout"])
        headers = {"User-Agent": "Mozilla/5.0 VLC/3.0"}

        async with aiohttp.ClientSession(cookie_jar=None) as session:
            start_time = time.time()
            async with session.get(url, timeout=timeout, headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    return False, None

                elapsed_time = time.time() - start_time
                content_type = response.headers.get("Content-Type", "").lower()

                # 黑名单直接排除
                if any(t in content_type for t in not_live_types):
                    return False, None

                is_live_type = any(t in content_type for t in live_types)

                # Content-Type 不明确时读取头部字节判断
                if not is_live_type:
                    try:
                        chunk = await asyncio.wait_for(response.content.read(512), timeout=5)
                    except asyncio.TimeoutError:
                        return False, None

                    if not chunk:
                        return False, None

                    # TS 流
                    if chunk[0] == 0x47:
                        return True, elapsed_time

                    # M3U8 流
                    if chunk.startswith(b'#EXTM3U') or chunk.startswith(b'#EXT'):
                        is_live_type = True
                    else:
                        return False, None

            # ✅ 如果是 m3u8 链接，额外检测是否为直播（非点播）
            is_m3u8_url = (
                url.endswith('.m3u8') or
                'm3u8' in url or
                'application/vnd.apple.mpegurl' in content_type or
                'application/x-mpegurl' in content_type
            )
            if is_m3u8_url:
                is_live = await check_m3u8_is_live(session, url, timeout)
                if not is_live:
                    return False, None

            return True, elapsed_time

    except asyncio.TimeoutError:
        return False, None
    except Exception:
        return False, None


async def test_multiple_streams(urls):
    semaphore = asyncio.Semaphore(CONFIG["max_parallel"])

    async def limited_test(url):
        async with semaphore:
            return await test_stream(url)

    tasks = [limited_test(url) for _, url in urls]
    results = await asyncio.gather(*tasks)
    return results


async def read_and_test_file(file_path, is_m3u=False):
    try:
        async with aiohttp.ClientSession(cookie_jar=None) as session:
            async with session.get(file_path, timeout=aiohttp.ClientTimeout(total=30)) as response:
                content = await response.text(errors="ignore")

        if is_m3u:
            entries = extract_urls_from_m3u(content)
        else:
            entries = extract_urls_from_txt(content)

        if not entries:
            return []

        print(f"📋 {file_path} 找到 {len(entries)} 个频道，检测中...")

        valid_urls = []
        results = await test_multiple_streams(entries)
        for (is_valid, _), (channel, url) in zip(results, entries):
            if is_valid:
                valid_urls.append((channel, url))

        print(f"  ✅ 有效: {len(valid_urls)} / {len(entries)}")
        return valid_urls

    except Exception as e:
        print(f"  ❌ 读取失败: {file_path} -> {e}")
        return []


def generate_sorted_m3u(valid_urls, cctv_channels, province_channels, filename):
    cctv_channels_list = []
    province_channels_list = defaultdict(list)
    satellite_channels = []
    other_channels = []

    for channel, url in valid_urls:
        if contains_date(channel) or contains_date(url):
            continue

        normalized_channel = normalize_cctv_name(channel)

        if normalized_channel in cctv_channels:
            cctv_channels_list.append({
                "channel": channel,
                "url": url,
                "logo": f"https://live.fanmingming.cn/tv/{channel}.png",
                "group_title": "央视频道"
            })
        elif "卫视" in channel:
            satellite_channels.append({
                "channel": channel,
                "url": url,
                "logo": f"https://live.fanmingming.cn/tv/{channel}.png",
                "group_title": "卫视频道"
            })
        else:
            found_province = False
            for province, channels in province_channels.items():
                for province_channel in channels:
                    if province_channel in channel:
                        province_channels_list[province].append({
                            "channel": channel,
                            "url": url,
                            "logo": f"https://live.fanmingming.cn/tv/{channel}.png",
                            "group_title": f"{province}"
                        })
                        found_province = True
                        break
                if found_province:
                    break
            if not found_province:
                other_channels.append({
                    "channel": channel,
                    "url": url,
                    "logo": f"https://live.fanmingming.cn/tv/{channel}.png",
                    "group_title": "其他频道"
                })

    for province in province_channels_list:
        province_channels_list[province].sort(key=lambda x: x["channel"])

    satellite_channels.sort(key=lambda x: x["channel"])
    other_channels.sort(key=lambda x: x["channel"])

    all_channels = cctv_channels_list + satellite_channels + \
                   [channel for province in sorted(province_channels_list) for channel in
                    province_channels_list[province]] + \
                   other_channels

    m3u8_filename = filename.replace('.m3u', '.m3u8')

    for fname in [filename, m3u8_filename]:
        with open(fname, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n")
            for channel_info in all_channels:
                f.write(
                    f"#EXTINF:-1 tvg-name=\"{channel_info['channel']}\" tvg-logo=\"{channel_info['logo']}\" group-title=\"{channel_info['group_title']}\",{channel_info['channel']}\n")
                f.write(f"{channel_info['url']}\n")

    print(f"📺 总计写入频道数: {len(all_channels)}")


async def main(file_urls, cctv_channel_file, province_channel_files):
    cctv_channels = load_cctv_channels(cctv_channel_file)
    province_channels = load_province_channels(province_channel_files)

    all_valid_urls = []

    for file_url in file_urls:
        if file_url.endswith(('.m3u', '.m3u8')):
            valid_urls = await read_and_test_file(file_url, is_m3u=True)
        elif file_url.endswith('.txt'):
            valid_urls = await read_and_test_file(file_url, is_m3u=False)
        else:
            valid_urls = []

        all_valid_urls.extend(valid_urls)

    generate_sorted_m3u(all_valid_urls, cctv_channels, province_channels, CONFIG["output_file"])
    print(f"🎉 Generated sorted M3U file: {CONFIG['output_file']}")


if __name__ == "__main__":
    file_urls = [
        "https://tzdr.com/iptv.txt",
        "https://live.kilvn.com/iptv.m3u",
        "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u",
        "https://gh-proxy.com/raw.githubusercontent.com/vbskycn/iptv/refs/heads/master/tv/iptv4.m3u",
        "http://175.178.251.183:6689/live.m3u",
        "https://m3u.ibert.me/ycl_iptv.m3u"
    ]

    cctv_channel_file = ".github/workflows/IPTV/CCTV.txt"

    province_channel_files = [
        ".github/workflows/IPTV/重庆频道.txt",
        ".github/workflows/IPTV/四川频道.txt",
        ".github/workflows/IPTV/云南频道.txt",
        ".github/workflows/IPTV/安徽频道.txt",
        ".github/workflows/IPTV/福建频道.txt",
        ".github/workflows/IPTV/甘肃频道.txt",
        ".github/workflows/IPTV/广东频道.txt",
        ".github/workflows/IPTV/广西频道.txt",
        ".github/workflows/IPTV/贵州频道.txt",
        ".github/workflows/IPTV/海南频道.txt",
        ".github/workflows/IPTV/河北频道.txt",
        ".github/workflows/IPTV/河南频道.txt",
        ".github/workflows/IPTV/黑龙江频道.txt",
        ".github/workflows/IPTV/湖北频道.txt",
        ".github/workflows/IPTV/湖南频道.txt",
        ".github/workflows/IPTV/吉林频道.txt",
        ".github/workflows/IPTV/江苏频道.txt",
        ".github/workflows/IPTV/江西频道.txt",
        ".github/workflows/IPTV/辽宁频道.txt",
        ".github/workflows/IPTV/内蒙频道.txt",
        ".github/workflows/IPTV/宁夏频道.txt",
        ".github/workflows/IPTV/青海频道.txt",
        ".github/workflows/IPTV/山东频道.txt",
        ".github/workflows/IPTV/山西频道.txt",
        ".github/workflows/IPTV/陕西频道.txt",
        ".github/workflows/IPTV/上海频道.txt",
        ".github/workflows/IPTV/天津频道.txt",
        ".github/workflows/IPTV/卫视频道.txt",
        ".github/workflows/IPTV/新疆频道.txt",
        ".github/workflows/IPTV/云南频道.txt",
        ".github/workflows/IPTV/浙江频道.txt",
        ".github/workflows/IPTV/北京频道.txt"
    ]

    asyncio.run(main(file_urls, cctv_channel_file, province_channel_files))
