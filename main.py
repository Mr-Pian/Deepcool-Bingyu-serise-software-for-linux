import usb.core
import usb.util
import time
import struct
import psutil
import socket
import os
import sys
import json
import threading
import argparse
import cv2
import atexit
from collections import deque
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# --- 全局配置 ---
SOCKET_PATH = "/tmp/deepcool.sock"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
CONFIG_LOCK = threading.Lock()


# --- 核心工具函数 ---
def load_settings():
    with CONFIG_LOCK:
        if not os.path.exists(CONFIG_FILE): return {}
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}


def update_settings(updates):
    with CONFIG_LOCK:
        current_data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    current_data = json.load(f)
            except:
                pass
        current_data.update(updates)
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_data, f, indent=2)
            try:
                os.chmod(CONFIG_FILE, 0o666)
            except:
                pass
        except:
            pass


def get_boot_id():
    """获取本次开机的唯一标识"""
    try:
        with open('/proc/sys/kernel/random/boot_id', 'r') as f:
            return f.read().strip()
    except:
        return "unknown"


def get_raw_uptime():
    """获取高精度系统运行时间"""
    try:
        with open('/proc/uptime', 'r') as f:
            return float(f.readline().split()[0])
    except:
        return time.time() - psutil.boot_time()


# --- 辅助函数：图像处理 ---
def resize_contain(pil_img, target_width=320, target_height=240, bg_color=(0, 0, 0)):
    width_ratio = target_width / pil_img.width
    height_ratio = target_height / pil_img.height
    scale = min(width_ratio, height_ratio)
    new_width = int(pil_img.width * scale)
    new_height = int(pil_img.height * scale)
    resample_method = getattr(Image, "Resampling", Image).LANCZOS
    img_resized = pil_img.resize((new_width, new_height), resample_method)
    new_img = Image.new("RGB", (target_width, target_height), bg_color)
    new_img.paste(img_resized, ((target_width - new_width) // 2, (target_height - new_height) // 2))
    return new_img


def process_frame_cv2(cv_frame, target_width=320, target_height=240, mode='contain'):
    h, w = cv_frame.shape[:2]
    scale = min(target_width / w, target_height / h) if mode == 'contain' else max(target_width / w, target_height / h)
    new_w, new_h = int(w * scale), int(h * scale)
    resized = cv2.resize(cv_frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    rgb_frame = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    pil_content = Image.fromarray(rgb_frame)
    if mode == 'cover':
        left = (new_w - target_width) // 2
        top = (new_h - target_height) // 2
        return pil_content.crop((left, top, left + target_width, top + target_height))
    else:
        new_img = Image.new("RGB", (target_width, target_height), (0, 0, 0))
        new_img.paste(pil_content, ((target_width - new_w) // 2, (target_height - new_h) // 2))
        return new_img


# --- 硬件监控类 ---
# ... (前面的 import 保持不变)

# --- 硬件监控类 (修复版) ---
class SystemMonitor:
    def __init__(self):
        self.hostname = socket.gethostname()
        self.usage_history = deque([0] * 60, maxlen=60)
        self.boot_time = psutil.boot_time()
        self.power_path = None
        self.last_rapl_energy = 0
        self.last_rapl_time = 0
        self.last_valid_power = 0.0

        # 累计时间逻辑
        self.last_save_time = time.time()
        settings = load_settings()
        saved_total = settings.get("total_seconds", 0)
        last_boot_id = settings.get("boot_id", "")
        current_boot_id = get_boot_id()
        current_uptime = get_raw_uptime()

        if current_boot_id == last_boot_id:
            self.history_base = max(0, saved_total - current_uptime)
        else:
            self.history_base = saved_total
            update_settings({"boot_id": current_boot_id})

        self._init_power_monitoring()
        atexit.register(self.force_save_runtime)

    def _init_power_monitoring(self):
        """初始化或重新寻找电源路径"""
        self.power_path = None # 重置
        rapl_path = '/sys/class/powercap/intel-rapl:0/energy_uj'
        # 优先尝试 Intel RAPL
        if os.path.exists(rapl_path) and os.access(rapl_path, os.R_OK):
            self.power_path = rapl_path
        else:
            # 备选: 尝试 AMD hwmon (例如 k10temp)
            # 这里可以根据需要扩展搜索逻辑
            pass

        if self.power_path:
            try:
                self.last_rapl_energy = int(self._read_file(self.power_path))
                self.last_rapl_time = time.time()
                # print(f"Power sensor linked: {self.power_path}")
            except: pass

    def _read_file(self, path):
        with open(path, 'r') as f: return f.read().strip()

    def force_save_runtime(self):
        current_total = self.history_base + get_raw_uptime()
        update_settings({"total_seconds": current_total, "boot_id": get_boot_id()})

    def get_total_runtime_str(self):
        now = time.time()
        current_uptime = get_raw_uptime()
        total_seconds = self.history_base + current_uptime

        if now - self.last_save_time > 60:
            self.force_save_runtime()
            self.last_save_time = now

        hours = int(total_seconds / 3600)
        return f"TOT: {hours}H"

    def get_uptime_str(self):
        uptime_seconds = get_raw_uptime()
        m, s = divmod(uptime_seconds, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)
        if d > 0: return f"UP: {int(d)}d {int(h):02}:{int(m):02}:{int(s):02}"
        return f"UP: {int(h):02}:{int(m):02}:{int(s):02}"

    def get_cpu_usage(self):
        usage = psutil.cpu_percent(interval=0)
        self.usage_history.append(usage)
        return usage

    def get_cpu_temp(self):
        temps = psutil.sensors_temperatures()
        if 'k10temp' in temps:
            for entry in temps['k10temp']:
                if entry.label in ['Tctl', 'Tdie']: return entry.current
        sensor_names = ['acpitz', 'zenpower']
        for name in sensor_names:
            if name in temps: return temps[name][0].current
        return 0.0

    def get_cpu_power(self):
        """
        获取 CPU 功耗 (修复死锁 Bug 版)
        """
        # 1. 如果路径丢失，尝试重新初始化 (自我修复)
        if not self.power_path:
            self._init_power_monitoring()
            if not self.power_path: return 0.0

        try:
            raw_val = int(self._read_file(self.power_path))
            current_time = time.time()
            time_delta = current_time - self.last_rapl_time

            # 防止除以零
            if time_delta < 0.01:
                return self.last_valid_power

            energy_delta = raw_val - self.last_rapl_energy

            # --- 核心修复逻辑 ---
            if energy_delta < 0:
                # 检测到计数器翻转 (Wrap-around) 或重置
                # 此时虽然不能计算本次的功率，但必须更新基准值！
                # 否则下一次循环 raw_val 依然很小，delta 依然为负，导致死锁
                self.last_rapl_energy = raw_val
                self.last_rapl_time = current_time
                # 返回上一次的有效值以平滑显示
                return self.last_valid_power

            if energy_delta == 0:
                # 传感器数值没变 (采样太快或传感器更新慢)
                # 不更新 time 和 energy，让 diff 累积到下一次，以提高精度
                return self.last_valid_power

            # 正常计算
            watts = (energy_delta / 1_000_000.0) / time_delta

            # 更新状态
            self.last_valid_power = watts
            self.last_rapl_energy = raw_val
            self.last_rapl_time = current_time

            return watts

        except Exception:
            # 读取失败 (可能是文件锁或权限问题)
            # 如果连续失败，可以考虑在这里加计数器触发 _init_power_monitoring
            return self.last_valid_power

# --- 屏幕驱动类 ---
class DeepCoolScreen:
    PACKET_HEADER = bytes.fromhex("aa08000001005802002c01bc11")
    WIDTH, HEIGHT, IMG_SIZE = 320, 240, 153600

    def __init__(self, vendor_id=0x3633, product_id=0x0026):
        self.dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
        if not self.dev: raise ValueError("Device not found")
        try:
            if self.dev.is_kernel_driver_active(0): self.dev.detach_kernel_driver(0)
            self.dev.set_configuration()
        except:
            pass
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(
            e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_out.write(bytes.fromhex("aa04000603640027b9"))
        time.sleep(0.01)
        self.ep_out.write(bytes.fromhex("aa0100092991"))
        time.sleep(0.01)
        self.ep_out.write(self.PACKET_HEADER)
        self.font_large = self._load_font(45)
        self.font_med = self._load_font(22)
        self.font_small = self._load_font(13)

    def _load_font(self, size):
        font_paths = [
            "/usr/share/fonts/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"
        ]
        for p in font_paths:
            if os.path.exists(p): return ImageFont.truetype(p, size)
        return ImageFont.load_default()

    def display(self, img):
        if img.size != (self.WIDTH, self.HEIGHT): img = img.resize((self.WIDTH, self.HEIGHT))
        pixels = img.getdata()
        buffer = bytearray(self.IMG_SIZE)
        idx = 0
        for r, g, b in pixels:
            r5, g6, b5 = (r >> 3) & 0x1F, (g >> 2) & 0x3F, (b >> 3) & 0x1F
            rgb565 = (r5 << 11) | (g6 << 5) | b5
            struct.pack_into('<H', buffer, idx, rgb565)
            idx += 2
        try:
            self.ep_out.write(self.PACKET_HEADER)
            self.ep_out.write(buffer)
        except:
            pass


# --- UI 绘制 ---
def draw_monitor_ui(screen_obj, monitor):
    image = Image.new("RGB", (320, 240), "#000000")
    draw = ImageDraw.Draw(image)
    temp, usage, power = monitor.get_cpu_temp(), monitor.get_cpu_usage(), monitor.get_cpu_power()

    C_BG, C_DIM, C_ACCENT = "#111111", "#777777", "#00CCFF"
    c_temp = "#00FF00"
    if temp > 55: c_temp = "#FFD700"
    if temp > 75: c_temp = "#FF3300"

    draw.rectangle((0, 0, 320, 28), fill=C_BG)

    # 1. Hostname (Left)
    host_str = f"{monitor.hostname.upper()}'s PC"
    draw.text((8, 5), host_str, font=screen_obj.font_small, fill="#FFFFFF")
    try:
        host_w = draw.textbbox((0, 0), host_str, font=screen_obj.font_small)[2]
    except:
        host_w = 80

    # 2. Total Runtime (Right)
    total_str = monitor.get_total_runtime_str()
    try:
        total_w = draw.textbbox((0, 0), total_str, font=screen_obj.font_small)[2]
    except:
        total_w = 60
    draw.text((320 - total_w - 8, 5), total_str, font=screen_obj.font_small, fill=C_ACCENT)

    # 3. Uptime (动态居中: 位于 Hostname 结束和 Total 开始之间的中心)
    left_end = 8 + host_w
    right_start = 320 - total_w - 8
    center_point = (left_end + right_start) // 2

    uptime_str = monitor.get_uptime_str()
    try:
        uptime_w = draw.textbbox((0, 0), uptime_str, font=screen_obj.font_small)[2]
    except:
        uptime_w = 80

    # 绘制坐标 = 中心点 - 文字一半宽度
    draw.text((center_point - (uptime_w // 2), 5), uptime_str, font=screen_obj.font_small, fill="#FFFFFF")

    LABEL_Y, CONTENT_Y, L_MARGIN, R_MARGIN = 40, 65, 30, 160

    draw.text((L_MARGIN + 17, LABEL_Y), "CPU TEMP", font=screen_obj.font_small, fill=C_DIM)
    arc_box = (L_MARGIN - 5, CONTENT_Y, L_MARGIN + 95, CONTENT_Y + 100)
    draw.arc(arc_box, 0, 360, "#222222", 5)
    draw.arc(arc_box, 270, 270 + int(360 * (temp / 100)), c_temp, 5)
    off_x = 0 if temp < 100 else -10
    draw.text((L_MARGIN + 15 + off_x, CONTENT_Y + 18), f"{int(temp)}°", font=screen_obj.font_large, fill="#FFFFFF")

    draw.text((R_MARGIN, LABEL_Y), "CPU LOAD", font=screen_obj.font_small, fill=C_DIM)
    draw.rectangle((R_MARGIN, CONTENT_Y, 300, CONTENT_Y + 12), fill="#222222")
    draw.rectangle((R_MARGIN, CONTENT_Y, R_MARGIN + int((300 - R_MARGIN) * (usage / 100)), CONTENT_Y + 12),
                   fill=C_ACCENT)
    draw.text((R_MARGIN, CONTENT_Y + 15), f"{usage:.1f}%", font=screen_obj.font_med, fill="#FFFFFF")

    draw.text((R_MARGIN, 115), "POWER", font=screen_obj.font_small, fill=C_DIM)
    draw.text((R_MARGIN, 135), f"{power:.1f} W", font=screen_obj.font_med, fill="#FFAA00")

    GH, GY = 50, 240
    draw.rectangle((0, GY - GH, 320, GY), fill="#080808")
    for i in range(1, 4): draw.line((0, GY - GH * i // 4, 320, GY - GH * i // 4), "#2A2A2A")
    for i in range(1, 5): draw.line((320 * i // 5, GY - GH, 320 * i // 5, GY), "#2A2A2A")
    draw.line((0, GY - GH, 320, GY - GH), "#333333")

    pts = []
    step = 320 / (len(monitor.usage_history) - 1)
    for i, val in enumerate(monitor.usage_history):
        y = int(GY - (val / 100 * GH))
        pts.append((int(i * step), min(max(y, GY - GH + 1), GY - 1)))
    if len(pts) > 1: draw.line(pts, fill=C_ACCENT, width=2)

    return image


# --- 服务端状态管理 ---
class ServiceState:
    def __init__(self):
        self.mode = "MONITOR"
        self.brightness = 1.0
        self.video_cap = None
        self.video_fps = 30.0
        self.static_image = None
        self.current_media_path = None
        self._init_from_settings()

    def _cleanup(self):
        if self.video_cap and self.video_cap.isOpened():
            self.video_cap.release()
            self.video_cap = None
        self.static_image = None

    def _init_from_settings(self):
        settings = load_settings()
        self.brightness = settings.get("brightness", 1.0)
        last_mode = settings.get("mode", "MONITOR")
        last_path = settings.get("media_path")
        if last_mode in ["VIDEO", "STATIC"] and last_path:
            success, _ = self.set_media(last_path)
            if not success: self.mode = "MONITOR"
        else:
            self.mode = "MONITOR"

    def set_media(self, path):
        if not os.path.exists(path): return False, "File not found"
        try:
            self._cleanup()
            cap = cv2.VideoCapture(path)
            if not cap.isOpened(): return False, "Failed to open media"

            ret, frame = cap.read()
            if not ret: return False, "Empty media"

            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)

            processed_img = process_frame_cv2(frame, 320, 240, mode='contain')

            if frame_count == 1 or fps <= 0:
                self.mode = "STATIC"
                self.static_image = processed_img
                cap.release()
                msg = "Static Image loaded"
            else:
                self.mode = "VIDEO"
                self.video_cap = cap
                self.video_fps = fps if fps > 0 else 30.0
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                msg = f"Video loaded ({self.video_fps} FPS)"

            self.current_media_path = path
            update_settings({"mode": self.mode, "media_path": path})
            return True, msg
        except Exception as e:
            return False, str(e)


# --- Socket Server ---
def server_thread(state):
    if os.path.exists(SOCKET_PATH):
        try:
            os.unlink(SOCKET_PATH)
        except:
            pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)
    os.chmod(SOCKET_PATH, 0o666)

    while True:
        try:
            conn, _ = server.accept()
            data = conn.recv(4096)
            if data:
                cmd = json.loads(data.decode())
                act = cmd.get('action')
                res = {"status": "ok"}

                if act == 'monitor':
                    state.mode = "MONITOR"
                    state._cleanup()
                    update_settings({"mode": "MONITOR"})
                elif act == 'media':
                    success, msg = state.set_media(cmd.get('path'))
                    if not success: res = {"status": "error", "message": msg}
                elif act == 'brightness':
                    val = max(0.0, min(1.0, cmd.get('value', 100) / 100.0))
                    state.brightness = val
                    update_settings({"brightness": val})

                conn.send(json.dumps(res).encode())
            conn.close()
        except:
            pass


# --- Client ---
def send_cmd(payload):
    if not os.path.exists(SOCKET_PATH):
        print("Error: Service not running")
        return
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(SOCKET_PATH)
        client.send(json.dumps(payload).encode())
        print("Server:", client.recv(4096).decode())
        client.close()
    except Exception as e:
        print(f"Connection failed: {e}")


# --- Main ---
def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--daemon", action="store_true")
    group.add_argument("--monitor", action="store_true")
    group.add_argument("--media", type=str, help="Play Image/Video/GIF")
    parser.add_argument("--brightness", type=int)
    args = parser.parse_args()

    if args.monitor or args.media or (args.brightness is not None):
        if args.monitor: send_cmd({"action": "monitor"})
        if args.media: send_cmd({"action": "media", "path": os.path.abspath(args.media)})
        if args.brightness is not None: send_cmd({"action": "brightness", "value": args.brightness})
        return

    print("DeepCool Service Started...")
    state, monitor = ServiceState(), SystemMonitor()
    try:
        screen = DeepCoolScreen()
    except:
        return

    t = threading.Thread(target=server_thread, args=(state,), daemon=True)
    t.start()

    try:
        while True:
            start = time.time()
            img = None

            if state.mode == "MONITOR":
                img = draw_monitor_ui(screen, monitor)
            elif state.mode == "STATIC":
                if state.static_image: img = state.static_image.copy()
            elif state.mode == "VIDEO":
                if state.video_cap and state.video_cap.isOpened():
                    ret, frame = state.video_cap.read()
                    if not ret:
                        state.video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = state.video_cap.read()
                    if ret:
                        img = process_frame_cv2(frame, 320, 240, mode='contain')

            if img:
                if state.brightness < 1.0:
                    img = ImageEnhance.Brightness(img).enhance(state.brightness)
                screen.display(img)

            wait = 0
            if state.mode == "VIDEO":
                wait = (1.0 / state.video_fps) - (time.time() - start)
            else:
                wait = 0.2 - (time.time() - start)

            if wait > 0: time.sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        if os.path.exists(SOCKET_PATH): os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()
