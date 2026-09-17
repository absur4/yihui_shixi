from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.dml.color import RGBColor
from pptx.enum.dml import MSO_THEME_COLOR
from pptx.enum.text import MSO_AUTO_SIZE
from copy import deepcopy
import os

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(BASE))
TEMPLATE = os.path.join(ROOT, '通用PPT.pptx')
OUT = os.path.join(BASE, '四种通信中间件性能对比测试_汇报.pptx')

prs = Presentation(TEMPLATE)
W, H = prs.slide_width, prs.slide_height
blank = prs.slide_layouts[6]

# Office theme colors used by the supplied master.
NAVY = RGBColor(31, 78, 121)
BLUE = RGBColor(68, 114, 196)
TEAL = RGBColor(0, 150, 160)
ORANGE = RGBColor(237, 125, 49)
GOLD = RGBColor(255, 192, 0)
INK = RGBColor(35, 45, 58)
MUTED = RGBColor(96, 108, 122)
PALE = RGBColor(242, 246, 250)
PALE_BLUE = RGBColor(232, 240, 249)
PALE_ORANGE = RGBColor(253, 241, 230)
WHITE = RGBColor(255, 255, 255)
GREEN = RGBColor(112, 173, 71)
RED = RGBColor(192, 0, 0)

FONT = 'Microsoft YaHei'
MONO = 'Consolas'

def rm_all_slides(prs):
    ids = list(prs.slides._sldIdLst)
    for sid in ids:
        rel_id = sid.rId
        prs.part.drop_rel(rel_id)
        prs.slides._sldIdLst.remove(sid)

rm_all_slides(prs)

def set_bg(slide, color=WHITE):
    fill = slide.background.fill
    fill.solid(); fill.fore_color.rgb = color

def shape(slide, kind, x, y, w, h, fill=WHITE, line=None, radius=False):
    sh = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid(); sh.fill.fore_color.rgb = fill
    sh.line.color.rgb = line if line else fill
    if line is None:
        sh.line.fill.background()
    return sh

def textbox(slide, x, y, w, h, text='', size=16, color=INK, bold=False,
            align=PP_ALIGN.LEFT, font=FONT, valign=MSO_ANCHOR.TOP, margin=0.06,
            italic=False):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.clear(); tf.word_wrap = True
    tf.margin_left = Inches(margin); tf.margin_right = Inches(margin)
    tf.margin_top = Inches(margin); tf.margin_bottom = Inches(margin)
    tf.vertical_anchor = valign
    p = tf.paragraphs[0]; p.alignment = align
    r = p.add_run(); r.text = text
    r.font.name = font; r.font.size = Pt(size); r.font.bold = bold; r.font.italic = italic
    r.font.color.rgb = color
    return tb

def richtext(slide, x, y, w, h, runs, size=16, color=INK, align=PP_ALIGN.LEFT,
             valign=MSO_ANCHOR.TOP, margin=0.06, font=FONT):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.clear(); tf.word_wrap = True
    tf.margin_left = Inches(margin); tf.margin_right = Inches(margin)
    tf.margin_top = Inches(margin); tf.margin_bottom = Inches(margin)
    tf.vertical_anchor = valign
    p = tf.paragraphs[0]; p.alignment = align
    for item in runs:
        txt = item[0]; b = item[1] if len(item)>1 else False; c = item[2] if len(item)>2 else color
        r = p.add_run(); r.text = txt; r.font.name = font; r.font.size = Pt(size); r.font.bold = b; r.font.color.rgb = c
    return tb

def add_header(slide, section, title, page, dark=False):
    if dark:
        set_bg(slide, NAVY)
        textbox(slide, 0.62, 0.38, 2.3, 0.28, section.upper(), 10, RGBColor(190,215,239), True)
        textbox(slide, 0.62, 0.82, 11.75, 0.6, title, 28, WHITE, True)
        shape(slide, MSO_SHAPE.RECTANGLE, 0.62, 1.52, 1.15, 0.07, ORANGE)
    else:
        set_bg(slide, WHITE)
        textbox(slide, 0.62, 0.30, 2.5, 0.25, section.upper(), 9, BLUE, True)
        textbox(slide, 0.62, 0.60, 11.75, 0.52, title, 25, NAVY, True)
        shape(slide, MSO_SHAPE.RECTANGLE, 0.62, 1.30, 1.1, 0.06, ORANGE)
    textbox(slide, 12.2, 7.08, 0.5, 0.2, f'{page:02d}', 9, RGBColor(150,160,170), True, PP_ALIGN.RIGHT)

def add_footer(slide, text='四种通信中间件性能对比测试'):
    shape(slide, MSO_SHAPE.RECTANGLE, 0.62, 6.98, 12.05, 0.012, RGBColor(222,228,235))
    textbox(slide, 0.62, 7.07, 5.6, 0.18, text, 8, MUTED)

def add_bullets(slide, x, y, w, h, items, size=14, color=INK, bullet_color=BLUE, gap=0.08):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame; tf.clear(); tf.word_wrap = True
    tf.margin_left = Inches(0.02); tf.margin_right = Inches(0.02); tf.margin_top = Inches(0.02); tf.margin_bottom = Inches(0.02)
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = 0; p.space_after = Pt(5); p.line_spacing = 1.08
        p.text = item
        p.font.name = FONT; p.font.size = Pt(size); p.font.color.rgb = color
        p._p.get_or_add_pPr().insert(0, __import__('lxml').etree.Element('{http://schemas.openxmlformats.org/drawingml/2006/main}buChar'))
        p._p.pPr[0].set('char', '•')
    return tb

def add_card(slide, x, y, w, h, title, body='', accent=BLUE, fill=PALE, title_size=16, body_size=12.5):
    sh = shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill, RGBColor(220,228,236))
    shape(slide, MSO_SHAPE.RECTANGLE, x, y, 0.07, h, accent)
    textbox(slide, x+0.18, y+0.12, w-0.3, 0.3, title, title_size, NAVY, True)
    if body:
        textbox(slide, x+0.18, y+0.49, w-0.3, h-0.56, body, body_size, INK)
    return sh

def add_pill(slide, x, y, w, text, fill=PALE_BLUE, color=NAVY):
    sh = shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, 0.31, fill, fill)
    textbox(slide, x, y+0.01, w, 0.25, text, 10.5, color, True, PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    return sh

def add_table(slide, x, y, w, h, data, col_widths=None, header_fill=NAVY, font_size=11, row_h=None):
    rows, cols = len(data), len(data[0])
    table = slide.shapes.add_table(rows, cols, Inches(x), Inches(y), Inches(w), Inches(h)).table
    if col_widths:
        for i, cw in enumerate(col_widths): table.columns[i].width = Inches(cw)
    for r in range(rows):
        if row_h: table.rows[r].height = Inches(row_h)
        for c in range(cols):
            cell = table.cell(r,c); cell.text = str(data[r][c])
            cell.margin_left = Inches(0.06); cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.04); cell.margin_bottom = Inches(0.04)
            cell.fill.solid(); cell.fill.fore_color.rgb = header_fill if r==0 else (PALE if r%2 else WHITE)
            for p in cell.text_frame.paragraphs:
                p.alignment = PP_ALIGN.CENTER if c>0 or r==0 else PP_ALIGN.LEFT
                for run in p.runs:
                    run.font.name = FONT; run.font.size = Pt(font_size); run.font.bold = (r==0 or c==0); run.font.color.rgb = WHITE if r==0 else (NAVY if c==0 else INK)
    return table

def add_image(slide, path, x, y, w=None, h=None):
    # Keep source aspect ratio if only one dimension is supplied.
    from PIL import Image
    im = Image.open(path); iw, ih = im.size
    if w and not h: h = w * ih / iw
    if h and not w: w = h * iw / ih
    return slide.shapes.add_picture(path, Inches(x), Inches(y), Inches(w), Inches(h))

def add_image_fit(slide, path, x, y, box_w, box_h):
    """Place an image inside a box without cropping or overflowing."""
    from PIL import Image
    iw, ih = Image.open(path).size
    scale = min(box_w / iw, box_h / ih)
    w, h = iw * scale, ih * scale
    return add_image(slide, path, x + (box_w-w)/2, y + (box_h-h)/2, w=w, h=h)

def app_icon(slide, x, y, label, sub, accent, kind):
    shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, 2.75, 2.02, WHITE, RGBColor(220,228,236))
    shape(slide, MSO_SHAPE.OVAL, x+0.18, y+0.18, 0.62, 0.62, accent)
    # simple, truthful visual cues rather than decorative stock imagery
    # Short ASCII labels keep the visual cue reliable across Chinese Office fonts.
    code = {'plane':'AV', 'iot':'IoT', 'robot':'ROS', 'chip':'RTPS'}.get(kind, 'SYS')
    textbox(slide, x+0.18, y+0.22, 0.62, 0.28, code, 13 if code in ('IoT','RTPS') else 17, WHITE, True, PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    textbox(slide, x+0.95, y+0.18, 1.62, 0.28, label, 16, NAVY, True)
    textbox(slide, x+0.18, y+0.90, 2.32, 0.74, sub, 11.5, INK)

def arrow(slide, x1, y1, x2, y2, color=BLUE, width=2.2):
    ln = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    ln.line.color.rgb = color; ln.line.width = Pt(width); ln.line.end_arrowhead = True
    return ln

# 1 Cover
s = prs.slides.add_slide(blank); add_header(s, '通信中间件性能评测', '四种通信中间件性能对比测试', 1, dark=True)
textbox(s, 0.65, 2.05, 7.6, 0.72, 'VSOA / MQTT / Zenoh / DDS', 31, WHITE, True)
textbox(s, 0.68, 2.95, 6.5, 0.74, '应用场所 · 原理 · 测试方案 · 实测结论', 19, RGBColor(214,228,242))
shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 8.5, 1.9, 3.8, 3.55, RGBColor(22,61,100), RGBColor(66,112,160))
for i,(name,col) in enumerate([('VSOA',ORANGE),('MQTT',GOLD),('Zenoh',TEAL),('DDS',BLUE)]):
    yy=2.25+i*0.68; shape(s, MSO_SHAPE.ROUNDED_RECTANGLE, 8.9, yy, 2.95, 0.45, col, col); textbox(s,8.9,yy+0.08,2.95,0.25,name,15,NAVY if i==1 else WHITE,True,PP_ALIGN.CENTER,margin=0)
textbox(s, 0.68, 6.15, 5.3, 0.3, '汇报：通信中间件测试小组', 12, RGBColor(190,215,239))
textbox(s, 10.0, 6.15, 2.3, 0.3, '2026.09', 12, RGBColor(190,215,239), True, PP_ALIGN.RIGHT)

# 2 application background
s=prs.slides.add_slide(blank); add_header(s,'01  应用背景','四种中间件服务于不同的通信现场',2); add_footer(s)
textbox(s,0.68,1.55,12.0,0.42,'它们并非同类替代品；应用场所决定了拓扑、可靠性和资源取舍。',15,INK)
app_icon(s,0.72,2.15,'VSOA','航空电子 / 工业运动控制\n车载电子 / 机器人控制',ORANGE,'plane')
app_icon(s,3.68,2.15,'MQTT','IoT 设备接入\n弱网遥测 / 远程监控',GOLD,'iot')
app_icon(s,6.64,2.15,'Zenoh','ROS 2 / 自动驾驶\n边缘计算 / 车路协同',TEAL,'robot')
app_icon(s,9.60,2.15,'DDS','航空航天 / ADAS\n工业自动化 / 医疗设备',BLUE,'chip')
add_card(s,0.72,4.62,3.72,1.32,'VSOA：强实时','微服务 + 发布订阅 + RPC 一体；小包高频控制。',ORANGE,PALE_ORANGE)
add_card(s,4.55,4.62,3.72,1.32,'MQTT：弱网接入','Broker 中继换取极简终端；QoS 分级可靠。',GOLD,RGBColor(255,248,225))
add_card(s,8.38,4.62,3.72,1.32,'Zenoh / DDS：广域与确定性','Zenoh 去中心；DDS 以数据为中心、QoS 丰富。',TEAL,PALE_BLUE)

# 3 evaluation dimensions
s=prs.slides.add_slide(blank); add_header(s,'01  应用背景','从应用场所推导四个正交评价维度',3); add_footer(s)
textbox(s,0.7,1.53,12.0,0.34,'应用场所 → 负载特征 → 测试维度，确保每个压力方向都有工程含义。',15,INK)
cards=[('速率','控制指令、状态回传','小包、稳定的高频发送','100–10,000 Hz',ORANGE),('尺寸','图像、点云、配置下发','单条消息体积跨度大','1 KiB–1 MiB',BLUE),('拓扑','多传感器汇聚 / 多订阅者','端数变化','1×1–4×4',TEAL),('时间','车机 / 工业现场长稳','持续负载','4 档 × 100 s',GOLD)]
for i,(a,b,c,d,col) in enumerate(cards):
    x=0.72+i*3.04; shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,x,2.05,2.72,3.78,WHITE,RGBColor(220,228,236)); shape(s,MSO_SHAPE.OVAL,x+0.88,2.32,0.95,0.95,col,col); textbox(s,x+0.88,2.59,0.95,0.30,a,16,NAVY if col==GOLD else WHITE,True,PP_ALIGN.CENTER,margin=0)
    textbox(s,x+0.22,3.48,2.28,0.45,b,14,NAVY,True,PP_ALIGN.CENTER); textbox(s,x+0.22,4.12,2.28,0.62,c,12,INK,False,PP_ALIGN.CENTER); add_pill(s,x+0.36,5.22,2.0,d,PALE_BLUE,NAVY)
arrow(s,3.42,6.20,4.08,6.20,ORANGE); arrow(s,6.46,6.20,7.12,6.20,ORANGE); arrow(s,9.50,6.20,10.16,6.20,ORANGE)
textbox(s,0.72,6.18,2.55,0.25,'应用共性需求',11,MUTED,True,PP_ALIGN.CENTER)
textbox(s,3.76,6.18,2.55,0.25,'负载特征',11,MUTED,True,PP_ALIGN.CENTER)
textbox(s,6.80,6.18,2.55,0.25,'测试因子',11,MUTED,True,PP_ALIGN.CENTER)
textbox(s,9.84,6.18,2.55,0.25,'可复现实验',11,MUTED,True,PP_ALIGN.CENTER)

# 4-7 principles
principles=[
('VSOA','原生微服务 / 发布订阅框架','TCP（本次测试）','应用层分片 60,000 B；1 MiB = 18 片','整包延迟由最慢分片决定；小包高频路径占优',ORANGE),
('MQTT','轻量级发布订阅，Broker 中继两跳','TCP；QoS 1','Broker 完整接收再转发；会话与重传','固定中继延迟；大消息多一次拷贝与排队',GOLD),
('Zenoh','Rust 实现；可 P2P 直连、无需 Broker','默认 TCP（本次配置）','Endpoint 直连；调度在 Rust runtime 内完成','中小消息延迟与 CPU 有优势；runtime 内存偏高',TEAL),
('DDS','以数据为中心的 RTPS 栈（Fast DDS 3.6.2）','UDPv4 + reliable','自动发现 + 丰富 QoS + RTPS 分片','QoS / history / flow controller 显著影响高负载',BLUE)
]
for idx,(name,pos,trans,mech,impact,col) in enumerate(principles, start=4):
    s=prs.slides.add_slide(blank); add_header(s,'02  原理',f'原理（{idx-3}）{name}：机制决定性能拐点',idx); add_footer(s)
    shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.72,1.68,2.2,4.9,col,col); textbox(s,0.92,2.18,1.8,0.55,name,28,WHITE if col!=GOLD else NAVY,True,PP_ALIGN.CENTER); textbox(s,0.92,3.05,1.8,1.22,pos,15,WHITE if col!=GOLD else NAVY,True,PP_ALIGN.CENTER)
    add_card(s,3.28,1.75,4.0,1.25,'传输与定位',f'{pos}\n{trans}',col,PALE_BLUE)
    add_card(s,7.55,1.75,4.95,1.25,'关键机制',mech,col,PALE_ORANGE if col in [ORANGE,GOLD] else PALE_BLUE)
    add_card(s,3.28,3.35,9.22,1.35,'对性能的直接影响',impact,col,PALE)
    # causal strip
    textbox(s,3.32,5.12,1.0,0.25,'机制',11,MUTED,True); add_pill(s,4.25,5.08,2.15,name+' 路径',PALE_BLUE,NAVY); arrow(s,6.55,5.23,7.25,5.23,col)
    add_pill(s,7.38,5.08,2.15,'延迟 / 吞吐 / 尾部',PALE_ORANGE,NAVY); arrow(s,9.68,5.23,10.38,5.23,col); add_pill(s,10.5,5.08,1.55,'实测差异',PALE_BLUE,NAVY)
    if name=='VSOA':
        textbox(s,3.32,5.72,8.9,0.36,'分片阈值 60,000 B：48 KiB 不分片，64 KiB 起切换路径。',13,ORANGE,True)
    elif name=='MQTT':
        textbox(s,3.32,5.72,8.9,0.36,'Broker 是数据转发中心：扇出时 CPU 成本集中在单点。',13,ORANGE,True)
    elif name=='Zenoh':
        textbox(s,3.32,5.72,8.9,0.36,'无中心转发：中小消息避免额外一跳，但 runtime 需要缓冲空间。',13,TEAL,True)
    else:
        textbox(s,3.32,5.72,8.9,0.36,'UDPv4 + reliable 与自动发现：确定性来自一致的 QoS 配置。',13,BLUE,True)

# 8 comparison table
s=prs.slides.add_slide(blank); add_header(s,'03  横向对比','四种实现的结构性差异',8); add_footer(s)
data=[['维度','VSOA','MQTT','Zenoh','DDS'],['实现形态','C++ + Python','C broker + Python','Rust','C++ + Python'],['通信拓扑','直连（Position）','Broker 中继（两跳）','P2P 直连','直连（自动发现）'],['传输（本次）','TCP','TCP','默认 TCP','UDPv4 / reliable'],['发现机制','Position 服务','Broker 会话','Endpoint 直配','自动发现'],['大消息策略','应用层分片 60,000 B','Broker 整体转发','原生传输','RTPS 分片'],['QoS 丰富度','少','QoS 0/1/2','优先级 / 可靠性','最丰富'],['中心节点','有（仅发现）','有（数据转发）','无','无']]
add_table(s,0.72,1.62,11.9,4.65,data,[2.0,2.45,2.45,2.45,2.55],font_size=10.5,row_h=0.50)
add_card(s,0.72,6.42,11.9,0.42,'读表方式','后续实测差异都应回到：拓扑、分片、中心节点、QoS 四条机理线索。',ORANGE,PALE_ORANGE,12,11)

# 9 scenario matrix
s=prs.slides.add_slide(blank); add_header(s,'04  测试方案','场景矩阵：每个场景只动一个影响因子',9); add_footer(s)
data=[['场景','影响因子','档位','主要指标'],['S01 点对点低延迟','发送速率','100 / 500 / 1000 / 2000 Hz','延迟、P95、P99'],['S02 消息大小扫描','payload','1 / 4 / 16 / 32 / 48 / 64 KiB','延迟、吞吐、丢包'],['S03 大消息吞吐','payload','64 / 128 / 256 / 512 KiB@20 Hz + 1 MiB@5 Hz','吞吐、P95、P99'],['S04 发送速率扫描','发送速率','100 / 1000 / 5000 / 10000 Hz','达成率、P99'],['S05 一对多扇出','订阅者数','1 / 2 / 3 / 4','交付、资源'],['S06 多对一汇聚','发布者数','1 / 2 / 3 / 4','交付、CPU'],['S07 多对多并发','P×S','1×1 … 4×4','吞吐、资源'],['S08 长时间稳定性','持续负载','4 档 × 100 s','漂移、内存']]
add_table(s,0.72,1.55,11.9,4.95,data,[1.55,1.75,5.2,3.4],font_size=9.5,row_h=0.50)
add_pill(s,0.80,6.47,3.4,'统一参数组 · 统一入口 · 可断点续跑',PALE_ORANGE,ORANGE); textbox(s,4.45,6.47,7.8,0.30,'拓扑默认固定为 1P / 1S，拓扑类场景除外。',11.5,MUTED)

# 10 metrics
s=prs.slides.add_slide(blank); add_header(s,'04  测试方案','统一指标口径：从逐样本到派生结论',10); add_footer(s)
data=[['编号','指标','公式 / 说明'],['U-1','单程延迟','(aᵢ − sᵢ) / 1e6（两端同一时基）'],['U-2 / U-3','均值 / 分位数','mean(d)；h=(n−1)k 线性插值'],['U-4','标准差','pstdev（总体标准差，ddof=0）'],['U-5','抖动','mean(|dᵢ − dᵢ₋₁|)，仅取相邻序号'],['U-6 / U-7','达成 / 应发吞吐','N × S × 8 / 窗口 / 1e6'],['U-8','丢包率','(E − N_recv) / E'],['U-9','达成率','达成吞吐 ÷ 应发吞吐']]
add_table(s,0.72,1.55,7.0,4.9,data,[1.3,1.7,4.0],font_size=11,row_h=0.58)
add_card(s,8.05,1.72,4.55,1.28,'净荷口径','吞吐不含协议头、不折算重传；统一使用十进制 1e6。',ORANGE,PALE_ORANGE)
add_card(s,8.05,3.18,4.55,1.28,'时间口径','warmup=1.0 s 不计统计；drain=2.0 s 仅收尾，不计丢包。',BLUE,PALE_BLUE)
add_card(s,8.05,4.64,4.55,1.28,'计数口径','一律取去重后计数；四家共用 mqtt/mqtt_metrics.py。',TEAL,PALE)

# 11 execution
s=prs.slides.add_slide(blank); add_header(s,'04  测试方案','执行环境与数据规模',11); add_footer(s)
add_card(s,0.72,1.62,3.65,1.32,'环境','Windows 本机回环\n16 逻辑核；同一解释器',BLUE,PALE_BLUE)
add_card(s,4.56,1.62,3.65,1.32,'数据规模','VSOA / MQTT / Zenoh：各 35 组\nDDS：32 组',ORANGE,PALE_ORANGE)
add_card(s,8.40,1.62,4.22,1.32,'运行方式','统一入口批量执行\n结果按组落盘，支持断点续跑',TEAL,PALE)
data=[['参数','取值','说明'],['拓扑','1P / 1S（拓扑类除外）','保证单变量'],['warmup_seconds','1.0','不计入统计'],['drain_seconds','2.0','四家统一，避免尾部误记'],['S08 时长','100 s / 档','四档覆盖轻到重负载'],['校验','status = completed','未完成组重跑补齐']]
add_table(s,0.72,3.35,7.1,2.75,data,[2.1,1.85,3.15],font_size=11,row_h=0.50)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,8.15,3.35,4.47,2.75,PALE,RGBColor(220,228,236)); textbox(s,8.44,3.62,3.9,0.28,'可回溯性',16,NAVY,True); textbox(s,8.44,4.08,3.8,1.45,'每组结果记录完整参数与指标，可回溯到原始逐样本。\n\n统一脚本保证四家在同一参数指纹下横向并表。',13,INK)

# result slides
result_specs=[
('延迟水平与可重复性','compare_repeatability.png','6 个场景中都存在同一条件（1P/1S、1 KiB、1000 Hz），构成天然交叉验证。','VSOA < MQTT < Zenoh：排序在 6 个场景中完全一致。','VSOA 延迟水平最低且重复性最好；MQTT 尾部最稳定；Zenoh 离散度最大。'),
('消息大小阶梯与分片拐点','compare_S02_payload_knee.png','竖向虚线 = VSOA 分片阈值 60,000 B；48 KiB 不分片，64 KiB 起切成 2 片。','48 → 64 KiB：VSOA 均值 +47%、P99 +70%；MQTT P99 ×7.5；Zenoh 基本无跳变。','这是机制切换点：从 48 KiB 到 64 KiB 不是量变，而是路径切换。'),
('大消息的尾部代价','compare_S03_large_message_tail.png','核心指标：P99 ÷ P95，衡量尾部被拉长了几倍。','1 MiB @5 Hz：VSOA 3.44×，MQTT 1.55×，Zenoh 1.09×。','VSOA 的 18 片分片放大尾部；Zenoh 尾部最平、均值也最低。'),
('拓扑成本结构','compare_S05_S07_topology.png','扇出、汇聚、并发 × 延迟 / 内存 / CPU。','吞吐随 payload × 速率 × P × S 线性增长；误差 <1%。','MQTT 扇出 CPU 涨幅最大；Zenoh 内存结构性偏高；延迟对拓扑规模弱敏感。'),
('长时间稳定性与漂移','compare_S08_drift.png','S08 组2：1 KiB @1000 Hz，100 s，按 10 s 分桶。','漂移：VSOA +2.9%，MQTT −1.6%，Zenoh −18.6%。','三家均无持续劣化；差异集中在进入稳态所需时间，Zenoh 约需 25 s 预热。')
]
for no,(title,img,method,headline,conclusion) in enumerate(result_specs,start=12):
    s=prs.slides.add_slide(blank); add_header(s,'05  测试结论',title,no); add_footer(s)
    p=os.path.join(BASE,img); add_image_fit(s,p,4.45,1.58,7.95,4.95)
    add_card(s,0.72,1.58,3.35,1.48,'方法',method,BLUE,PALE_BLUE,15,11.2)
    add_card(s,0.72,3.26,3.35,1.62,'核心发现',headline,ORANGE,PALE_ORANGE,15,12)
    add_card(s,0.72,5.10,3.35,1.08,'结论',conclusion,TEAL,PALE,15,11.2)
    textbox(s,4.48,6.52,7.6,0.26,'图：songfei/test/' + img, 8.5, MUTED, italic=True)

# 17 summary
s=prs.slides.add_slide(blank); add_header(s,'05  测试结论','四条可横向引用的结论',17); add_footer(s)
summary=[('01','延迟排序稳定','VSOA < MQTT < Zenoh','6 个场景交叉验证一致',BLUE),('02','VSOA 有明确切换点','48→64 KiB：均值 +47%、P99 +70%','S02 六档阶梯；四家同参数',ORANGE),('03','大消息尾部代价','VSOA 3.44× · MQTT 1.55× · Zenoh 1.09×','S03 五档；与分片机制对应',TEAL),('04','资源成本结构不同','Zenoh 内存偏高；MQTT 扇出 CPU 涨幅最大','拓扑类场景三组一致',GOLD)]
for i,(num,t,main,sub,col) in enumerate(summary):
    y=1.62+i*1.28; shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.72,y,1.02,0.86,col,col); textbox(s,0.72,y+0.22,1.02,0.28,num,19,NAVY if col==GOLD else WHITE,True,PP_ALIGN.CENTER,margin=0); textbox(s,1.98,y+0.07,3.4,0.28,t,15,NAVY,True); textbox(s,1.98,y+0.42,5.2,0.25,main,16,col if col!=GOLD else ORANGE,True); textbox(s,7.25,y+0.25,5.1,0.25,sub,12.3,INK)
textbox(s,0.72,6.72,11.6,0.24,'工程含义：先按场景选择通信模型，再用统一口径比较实现；不要脱离拓扑与消息尺寸谈“谁更快”。',12.5,NAVY,True)

# 18 appendix
s=prs.slides.add_slide(blank); add_header(s,'附录','数据文件与复现实验索引',18,dark=True)
textbox(s,0.72,1.74,5.3,0.34,'本汇报引用的真实实验图片',16,WHITE,True)
files=['compare_repeatability.png','compare_S02_payload_knee.png','compare_S03_large_message_tail.png','compare_S05_S07_topology.png','compare_S08_drift.png']
for i,f in enumerate(files):
    y=2.28+i*0.58; add_pill(s,0.78,y,0.55,f'图{i+1}',PALE_ORANGE,ORANGE); textbox(s,1.48,y+0.04,4.9,0.24,f,12,RGBColor(222,235,247))
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,7.05,1.70,5.50,3.85,RGBColor(22,61,100),RGBColor(66,112,160)); textbox(s,7.38,2.06,4.75,0.32,'复现实验要点',17,WHITE,True); add_bullets(s,7.42,2.62,4.55,2.2,['四家共用统一参数指纹','warmup / drain 口径一致','只采信 status = completed','结果可回溯到逐样本'],14,RGBColor(230,240,250))
textbox(s,0.72,6.30,11.5,0.42,'母版：通用PPT.pptx · 主题色沿用 Office 蓝 / 橙 · 版式重新组织为技术汇报结构',12,RGBColor(190,215,239))

prs.save(OUT)
print(OUT)
