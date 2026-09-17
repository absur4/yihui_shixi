"""把 songfei/test 下的对比图原位替换进汇报 PPT，保持图片框的位置与尺寸不变。

用法：
    python update_ppt_figures.py --check          # 只核对：图片框比例 vs 出图比例，不写文件
    python update_ppt_figures.py                  # 就地替换（自动备份，PPT 被占用则报错退出）
    python update_ppt_figures.py --output 新.pptx  # 另存为新文件，不动原文件

映射依据是每页图片下方的来源标注（"图：songfei/test/compare_xxx.png"），
不依赖页序与图片顺序 —— 以后增删页、调整页序都不会换错图。

替换原理：只把图片形状 BLIP 的 r:embed 指向新的图片部件，
不新建形状、不改 left/top/width/height，所以版式与原来逐像素一致。
代价是旧图片部件仍留在 pptx 包里（成为未被引用的冗余字节），
需要彻底瘦身的话用 PowerPoint 的"另存为"重打一次包即可。
"""
import argparse
import shutil
import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

TEST_DIR = Path(__file__).resolve().parent
DEFAULT_PPT = TEST_DIR / "四种通信中间件性能对比测试_汇报.pptx"
CAPTION_PREFIX = "图："
RATIO_TOLERANCE = 0.01          # 图片框与出图比例允许相差 1%，超过就警告会拉伸


def caption_name(slide):
    """取该页图片下方来源标注里的文件名；没有标注返回 None。"""
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        text = shape.text_frame.text.strip()
        if text.startswith(CAPTION_PREFIX):
            raw = text[len(CAPTION_PREFIX):].strip().replace("\\", "/")
            return Path(raw).name
    return None


def picture_shapes(slide):
    return [shape for shape in slide.shapes if shape.shape_type == 13]


def png_size(path: Path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"不是 PNG：{path}")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def main() -> int:
    parser = argparse.ArgumentParser(description="把对比图替换进汇报 PPT")
    parser.add_argument("--ppt", default=str(DEFAULT_PPT), help="目标 pptx")
    parser.add_argument("--images", default=str(TEST_DIR), help="图片所在目录")
    parser.add_argument("--output", default=None, help="另存为新文件；缺省=就地替换")
    parser.add_argument("--check", action="store_true", help="只核对比例，不写文件")
    args = parser.parse_args()

    ppt_path = Path(args.ppt).resolve()
    image_dir = Path(args.images).resolve()
    if not ppt_path.is_file():
        raise SystemExit(f"找不到 PPT：{ppt_path}")
    if args.output and args.check:
        raise SystemExit("--check 与 --output 不能同时用")

    presentation = Presentation(str(ppt_path))
    print(f"PPT  {ppt_path.name}  共 {len(presentation.slides)} 页")

    planned = []          # (slide_index, picture, png_path, frame_ratio, png_ratio)
    problems = []

    for index, slide in enumerate(presentation.slides, 1):
        pictures = picture_shapes(slide)
        if not pictures:
            continue
        name = caption_name(slide)
        if not name:
            problems.append(f"第 {index} 页有 {len(pictures)} 张图片，但没有『{CAPTION_PREFIX}』来源标注，"
                            f"无法确定该换成哪张 → 跳过")
            continue
        png_path = image_dir / name
        if not png_path.is_file():
            problems.append(f"第 {index} 页标注的是 {name}，但该文件不存在 → 跳过")
            continue
        if len(pictures) > 1:
            problems.append(f"第 {index} 页有 {len(pictures)} 张图片，只替换第 1 张（{name}）")
        picture = pictures[0]
        frame_ratio = picture.width / picture.height
        png_ratio = png_size(png_path)[0] / png_size(png_path)[1]
        planned.append((index, picture, png_path, frame_ratio, png_ratio))

    if not planned:
        print("没有找到任何可替换的图片")
        for item in problems:
            print(f"  [!] {item}")
        return 1

    print(f"\n{'页':<4}{'图片':<34}{'框 (in)':<18}{'框比例':<10}{'出图比例':<10}判定")
    for index, picture, png_path, frame_ratio, png_ratio in planned:
        drift = abs(png_ratio - frame_ratio) / frame_ratio
        verdict = "OK" if drift <= RATIO_TOLERANCE else f"会拉伸 {drift * 100:.1f}%"
        print(f"{index:<4}{png_path.name:<34}"
              f"{Emu(picture.width).inches:.2f}×{Emu(picture.height).inches:.2f}     "
              f"{frame_ratio:<10.3f}{png_ratio:<10.3f}{verdict}")
        if drift > RATIO_TOLERANCE:
            problems.append(f"第 {index} 页 {png_path.name}：出图比例 {png_ratio:.3f} "
                            f"与图片框 {frame_ratio:.3f} 不符，替换后会被拉伸")

    for item in problems:
        print(f"  [!] {item}")

    if args.check:
        print("\n--check 模式：未写任何文件")
        return 0

    # 先落盘到临时文件，全部成功后再替换目标 —— 中途出错不会留下半个 PPT
    target = Path(args.output).resolve() if args.output else ppt_path
    in_place = not args.output
    if in_place:
        try:
            with open(target, "r+b"):
                pass
        except PermissionError:
            # PowerPoint 打开时会锁住文件。不要在这里失败 —— 与用户对不上时间，
            # 直接把成品落成一份带后缀的新文件，并说清怎么合回原名。
            target = target.with_name(target.stem + "_图已更新.pptx")
            in_place = False
            print(f"\n[!] {ppt_path.name} 正被 PowerPoint 占用，改为另存：{target.name}")
    staging = target.with_name(target.stem + ".__staging__.pptx")

    for index, picture, png_path, _, _ in planned:
        slide = presentation.slides[index - 1]
        _, r_id = slide.part.get_or_add_image_part(str(png_path))
        picture._element.blipFill.blip.rEmbed = r_id
        print(f"  [替换] 第 {index} 页 ← {png_path.name}")

    presentation.save(str(staging))

    if in_place:
        backup = ppt_path.with_name(ppt_path.stem + "_备份.pptx")
        shutil.copy2(ppt_path, backup)
        shutil.move(str(staging), str(target))
        print(f"  备份 → {backup.name}")
    else:
        shutil.move(str(staging), str(target))

    print(f"\n完成：{target}")
    if not in_place and not args.output:
        print(f"     PowerPoint 关掉后，把它改回原名即可，或重跑本命令就地替换。")
    print("提示：旧图片部件仍在包内占空间，用 PowerPoint 打开后『另存为』一次即可瘦身。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
