import usb.core
import usb.util
import time
import struct
import psutil
import socket
import datetime
import os
from collections import deque
from PIL import Image, ImageDraw, ImageFont


# --- 硬件监控类 ---
class SystemMonitor:
    def __init__(self):
        self.hostname = socket.gethostname()
        self.usage_history = deque([0] * 60, maxlen=60)
        self.boot_time = psutil.boot_time()
        self.power_path = None
        self.last_rapl_energy = 0
        self.last_rapl_time = 0
        self.last_valid_power = 0.0
        self._init_power_monitoring()

    def _init_power_monitoring(self):
        rapl_path = '/sys/class/powercap/intel-rapl:0/energy_uj'
        if os.path.exists(rapl_path):
            if os.access(rapl_path, os.R_OK):
                self.power_path = rapl_path
                try:
                    self.last_rapl_energy = int(self._read_file(rapl_path))
                    self.last_rapl_time = time.time()
                    print(f"Power Monitor: 已加载系统电源接口 (RAPL)")
                except:
                    print("Power Monitor: 接口读取失败")
            else:
                print(f"Power Monitor: 无权限读取电源接口 (请使用 sudo)")

    def _read_file(self, path):
        with open(path, 'r') as f:
            return f.read().strip()

    def get_uptime_str(self):
        """
        获取精确到秒的运行时间
        格式: UP: 1d 05:30:59 或 UP: 05:30:59
        """
        diff = time.time() - self.boot_time
        m, s = divmod(diff, 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)

        # 这里加上了 :{int(s):02} 来显示秒
        if d > 0:
            return f"UP: {int(d)}d {int(h):02}:{int(m):02}:{int(s):02}"
        return f"UP: {int(h):02}:{int(m):02}:{int(s):02}"

    def get_cpu_usage(self):
        usage = psutil.cpu_percent(interval=0)
        self.usage_history.append(usage)
        return usage

    def get_cpu_temp(self):
        temps = psutil.sensors_temperatures()
        if 'k10temp' in temps:
            for entry in temps['k10temp']:
                if entry.label in ['Tctl', 'Tdie']:
                    return entry.current
        sensor_names = ['acpitz', 'zenpower']
        for name in sensor_names:
            if name in temps:
                return temps[name][0].current
        return 0.0

    def get_cpu_power(self):
        if not self.power_path: return 0.0
        try:
            raw_val = int(self._read_file(self.power_path))
            current_time = time.time()
            time_delta = current_time - self.last_rapl_time
            if time_delta < 0.05: return self.last_valid_power
            energy_delta = raw_val - self.last_rapl_energy
            if energy_delta < 0:
                self.last_rapl_energy = raw_val
                self.last_rapl_time = current_time
                return self.last_valid_power
            if energy_delta == 0: return self.last_valid_power
            watts = (energy_delta / 1_000_000.0) / time_delta
            self.last_valid_power = watts
            self.last_rapl_energy = raw_val
            self.last_rapl_time = current_time
            return watts
        except Exception:
            return self.last_valid_power


# --- 屏幕驱动类 (保持不变) ---
class DeepCoolScreen:
    PACKET_HEADER = bytes.fromhex("aa08000001005802002c01bc11")
    WIDTH = 320
    HEIGHT = 240
    IMG_SIZE = 153600

    def __init__(self, vendor_id=0x3633, product_id=0x0026):
        self.dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
        if not self.dev: raise ValueError("未找到设备")
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
            self.dev.set_configuration()
        except:
            pass

        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: \
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        )

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
        if img.size != (self.WIDTH, self.HEIGHT):
            img = img.resize((self.WIDTH, self.HEIGHT))

        pixels = img.getdata()
        buffer = bytearray(self.IMG_SIZE)
        idx = 0
        for r, g, b in pixels:
            r5 = (r >> 3) & 0x1F
            g6 = (g >> 2) & 0x3F
            b5 = (b >> 3) & 0x1F
            rgb565 = (r5 << 11) | (g6 << 5) | b5
            struct.pack_into('<H', buffer, idx, rgb565)
            idx += 2
        try:
            self.ep_out.write(self.PACKET_HEADER)
            self.ep_out.write(buffer)
        except usb.core.USBError:
            pass


# --- 界面绘制 ---
def draw_ui(screen_obj, monitor):
    image = Image.new("RGB", (320, 240), "#000000")
    draw = ImageDraw.Draw(image)

    temp = monitor.get_cpu_temp()
    usage = monitor.get_cpu_usage()
    power = monitor.get_cpu_power()
    uptime_str = monitor.get_uptime_str()

    COLOR_BG_HEADER = "#111111"
    COLOR_TEXT_DIM = "#777777"
    COLOR_ACCENT = "#00CCFF"

    color_temp = "#00FF00"
    if temp > 55: color_temp = "#FFD700"
    if temp > 75: color_temp = "#FF3300"

    # === 1. 顶部信息栏 ===
    draw.rectangle((0, 0, 320, 28), fill=COLOR_BG_HEADER)
    draw.text((8, 5), f"{monitor.hostname.upper()}'s PC", font=screen_obj.font_small, fill="#FFFFFF")
    try:
        bbox = draw.textbbox((0, 0), uptime_str, font=screen_obj.font_small)
        text_w = bbox[2] - bbox[0]
    except:
        text_w = 80
    # 这里颜色我改回了白色，如果你想要之前的蓝色，可以改成 fill=COLOR_ACCENT
    draw.text((320 - text_w - 8, 5), uptime_str, font=screen_obj.font_small, fill="#FFFFFF")

    # === 定义对齐坐标系 ===
    LABEL_Y = 40  # 左右两边标题统一高度
    CONTENT_Y_START = 65  # 左右两边内容统一起始高度
    LEFT_MARGIN = 30  # 左边块起始 X
    RIGHT_MARGIN = 160  # 右边块起始 X (屏幕中间)

    # === 2. 左侧: CPU 温度 ===
    # 标题
    draw.text((LEFT_MARGIN + 17, LABEL_Y), "CPU TEMP", font=screen_obj.font_small, fill=COLOR_TEXT_DIM)

    # 圆环
    arc_box = (LEFT_MARGIN - 5, CONTENT_Y_START, LEFT_MARGIN + 95, CONTENT_Y_START + 100)
    draw.arc(arc_box, start=0, end=360, fill="#222222", width=5)
    start_angle = 270
    sweep_angle = int(360 * (temp / 100))
    end_angle = start_angle + sweep_angle
    draw.arc(arc_box, start=start_angle, end=end_angle, fill=color_temp, width=5)

    # 数值
    t_str = f"{int(temp)}°"
    offset_x = 0 if temp < 100 else -10
    draw.text((LEFT_MARGIN + 15 + offset_x, CONTENT_Y_START + 18), t_str, font=screen_obj.font_large, fill="#FFFFFF")

    # === 3. 右侧: 负载与功耗 ===

    # --- CPU LOAD 块 ---
    draw.text((RIGHT_MARGIN, LABEL_Y), "CPU LOAD", font=screen_obj.font_small, fill=COLOR_TEXT_DIM)

    bar_height = 12
    draw.rectangle((RIGHT_MARGIN, CONTENT_Y_START, 300, CONTENT_Y_START + bar_height), fill="#222222")
    bar_len = int((300 - RIGHT_MARGIN) * (usage / 100))
    draw.rectangle((RIGHT_MARGIN, CONTENT_Y_START, RIGHT_MARGIN + bar_len, CONTENT_Y_START + bar_height),
                   fill=COLOR_ACCENT)

    draw.text((RIGHT_MARGIN, CONTENT_Y_START + 15), f"{usage:.1f}%", font=screen_obj.font_med, fill="#FFFFFF")

    # --- POWER 块 ---
    POWER_Y_START = 115

    draw.text((RIGHT_MARGIN, POWER_Y_START), "POWER", font=screen_obj.font_small, fill=COLOR_TEXT_DIM)
    p_str = f"{power:.1f} W"
    draw.text((RIGHT_MARGIN, POWER_Y_START + 20), p_str, font=screen_obj.font_med, fill="#FFAA00")

    # === 4. 底部: 历史波形图 ===
    graph_h = 50
    graph_y_base = 240
    graph_top_y = graph_y_base - graph_h

    draw.rectangle((0, graph_top_y, 320, 240), fill="#080808")

    # 网格线
    COLOR_GRID = "#2A2A2A"
    for i in range(1, 4):
        y_grid = int(graph_y_base - (graph_h * i / 4))
        draw.line((0, y_grid, 320, y_grid), fill=COLOR_GRID)
    for i in range(1, 5):
        x_grid = int(320 * i / 5)
        draw.line((x_grid, graph_top_y, x_grid, graph_y_base), fill=COLOR_GRID)
    draw.line((0, graph_top_y, 320, graph_top_y), fill="#333333")

    # 曲线
    pts = []
    step = 320 / (len(monitor.usage_history) - 1)
    for i, val in enumerate(monitor.usage_history):
        x = int(i * step)
        y = int(graph_y_base - (val / 100 * graph_h))
        if y >= graph_y_base: y = graph_y_base - 1
        if y <= graph_top_y: y = graph_top_y + 1
        pts.append((x, y))

    if len(pts) > 1:
        draw.line(pts, fill=COLOR_ACCENT, width=2)

    return image


def main():
    try:
        # time.sleep(5)
        print("Dashboard Started. (Refresh: 5Hz)")
        monitor = SystemMonitor()
        screen = DeepCoolScreen()

        TARGET_INTERVAL = 0.2

        while True:
            loop_start = time.time()
            img = draw_ui(screen, monitor)
            screen.display(img)
            elapsed = time.time() - loop_start
            sleep_time = TARGET_INTERVAL - elapsed
            if sleep_time > 0: time.sleep(sleep_time)

    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
    finally:
        pass


if __name__ == "__main__":
    main()