import os
import aiohttp
import asyncio
import time
from collections import defaultdict
import re
from datetime import datetime, timedelta


def get_dynamic_keywords():
    """
    动态生成需要过滤的关键词（今天的日期、明天的日期以及固定关键词）
    """
    fixed_keywords = ["免费提供"]
    return fixed_keywords

def contains_date(text):
    """
    检测字符串中是否包含日期格式（如 YYYY-MM-DD）
    """
    date_pattern = r"\d{4}-\d{2}-\d{2}"  # 正则表达式匹配 YYYY-MM-DD
    return re.search(date_pattern, text) is not None


# 配置
CONFIG = {
    "timeout": 10,  # Timeout in seconds
    "max_parallel": 30,  # Max concurrent requests
    "output_file": "best_sorted.m3u",  # Output file for the sorted M3U
    "iptv_directory": "IPTV"  # Directory containing IPTV files
}


# 读取 CCTV 频道列表
def load_cctv_channels(file_path=".github/workflows/IPTV/CCTV.txt"):
    """从文件加载 CCTV 频道列表"""
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


# 读取 IPTV 目录下所有省份频道文件
def load_province_channels(files):
    """加载多个省份的频道列表"""
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


# 正规化 CCTV 频道名称
def normalize_cctv_name(channel_name):
    """将 CCTV 频道名称进行正规化，例如 CCTV-1 -> CCTV1"""
    return re.sub(r'CCTV[-]?(\d+)', r'CCTV\1', channel_name)


# 从 TXT 文件中提取 IPTV 链接
def extract_urls_from_txt(content):
    """从 TXT 文件中提取 IPTV 链接"""
    urls = []
    for line in content.splitlines():
        line = line.strip()
        if line and ',' in line:
            parts = line.split(',', 1)
            urls.append(parts)
    return urls


# 从 M3U 文件中提取 IPTV 链接
def extract_urls_from_m3u(content):
    """从 M3U 文件中提取 IPTV 链接"""
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


# ✅ 改造后的 test_stream：同时检测可用性 + 是否为直播流
async def test_stream(url):
    """
    测试 IPTV 链接：
    1. HTTP 状态必须是 200
    2. Content-Type 必须是视频/音频流类型
    3. 如果 Content-Type 不明确，读取头部字节判断是否为 TS 或 M3U8 流
    返回 (True, 响应时间) 或 (False, None)
    """
    # Content-Type 白名单（直播流）
    live_types = [
        "video/",
        "audio/",
        "application/vnd.apple.mpegurl",  # m3u8
        "application/x-mpegurl",          # m3u8
        "application/octet-stream",       # 通用二进制流
        "video/mp2t",                     # TS 流
        "application/x-www-form-urlencoded",
    ]
    # Content-Type 黑名单（明确不是直播）
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

                # 白名单直接通过
                if any(t in content_type for t in live_types):
                    return True, elapsed_time

                # Content-Type 不明确时，读取头部字节判断
                try:
                    chunk = await asyncio.wait_for(response.content.read(512), timeout=5)
                except asyncio.TimeoutError:
                    return False, None

                if not chunk:
                    return False, None

                # TS 流：每个包以 0x47 开头
                if chunk[0] == 0x47:
                    return True, elapsed_time

                # M3U8 流：文本以 #EXTM3U 开头
                if chunk.startswith(b'#EXTM3U') or chunk.startswith(b'#EXT'):
                    return True, elapsed_time

                # 其他情况视为无效
                return False, None

    except asyncio.TimeoutError:
        return False, None
    except Exception:
        return False, None


# 测试多个 IPTV 链接
async def test_multiple_streams(urls):
    """测试多个 IPTV 链接"""
    semaphore = asyncio.Semaphore(CONFIG["max_parallel"])

    async def limited_test(url):
        async with semaphore:
            return await test_stream(url)

    tasks = [limited_test(url) for _, url in urls]
    results = await asyncio.gather(*tasks)
    return results


# 读取文件并提取 URL（支持 M3U 或 TXT 格式）
async def read_and_test_file(file_path, is_m3u=False):
    """读取文件并提取 URL 进行测试"""
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


# 生成排序后的 M3U 和 M3U8 文件
def generate_sorted_m3u(valid_urls, cctv_channels, province_channels, filename):
    """生成排序后的 M3U 和 M3U8 文件"""
    cctv_channels_list = []
    province_channels_list = defaultdict(list)
    satellite_channels = []
    other_channels = []
    keywords = get_dynamic_keywords()

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


# 主函数
async def main(file_urls, cctv_channel_file, province_channel_files):
    """主函数处理多个文件"""
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
