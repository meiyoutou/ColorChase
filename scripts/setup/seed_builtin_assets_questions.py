"""Seed built-in assets, selection questions, and area questions into MySQL."""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiomysql


BASE_DIR = Path(__file__).resolve().parents[1]


def load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mysql_config() -> dict:
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("COLORCHASE_DATABASE_URL is required")
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": parsed.path.lstrip("/"),
        "charset": "utf8mb4",
        "autocommit": False,
    }


def file_asset(
    slug: str,
    title: str,
    asset_type: str,
    rel_path: str,
    public_url: str | None = None,
    metadata: dict | None = None,
    sort_order: int = 0,
) -> dict:
    path = BASE_DIR / rel_path
    return {
        "slug": slug,
        "title": title,
        "asset_type": asset_type,
        "file_path": str(path),
        "public_url": public_url,
        "mime_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "metadata": metadata or {},
        "is_active": 1,
        "sort_order": sort_order,
    }


def virtual_asset(
    slug: str,
    title: str,
    asset_type: str,
    uri: str,
    metadata: dict | None = None,
    sort_order: int = 0,
) -> dict:
    return {
        "slug": slug,
        "title": title,
        "asset_type": asset_type,
        "file_path": uri,
        "public_url": None,
        "mime_type": "application/x-colorchase-profile",
        "size_bytes": 0,
        "metadata": metadata or {},
        "is_active": 1,
        "sort_order": sort_order,
    }


BUILTIN_ASSETS = [
    file_asset("static_logo", "ColorChase 标志", "static_image", "static/assets/logo.png", "/static/assets/logo.png", {"role": "brand"}, 10),
    file_asset("static_icon_small", "ColorChase 小图标", "static_image", "static/assets/icon-small.png", "/static/assets/icon-small.png", {"role": "app_icon"}, 20),
    file_asset("static_style_icon", "风格预设图标", "static_image", "static/assets/style-icon.png", "/static/assets/style-icon.png", {"role": "style_panel"}, 30),
    virtual_asset("profile_bw", "黑白胶片内置预设", "style_profile", "builtin://profile/bw", {"profile_id": "bw", "tone": "monochrome"}, 40),
    virtual_asset("profile_warm", "暖调内置预设", "style_profile", "builtin://profile/warm", {"profile_id": "warm", "tone": "warm"}, 50),
    virtual_asset("profile_cool", "冷调内置预设", "style_profile", "builtin://profile/cool", {"profile_id": "cool", "tone": "cool"}, 60),
    file_asset("profile_orange_bw", "橙黑内置预设 LUT", "style_profile", "presets/orange_bw.npy", None, {"profile_id": "orange_bw", "format": "npy_lut"}, 70),
    file_asset("style_ec8_lut_cube", "EC8A3572 全局 LUT Cube", "style_lut", "styles/extracted/EC8A3572-DxO_DeepPRIME_3/lut_global.cube", "/styles/extracted/EC8A3572-DxO_DeepPRIME_3/lut_global.cube", {"format": "cube"}, 80),
    file_asset("style_ec8_lut_npy", "EC8A3572 全局 LUT NPY", "style_lut", "styles/extracted/EC8A3572-DxO_DeepPRIME_3/lut_global.npy", "/styles/extracted/EC8A3572-DxO_DeepPRIME_3/lut_global.npy", {"format": "npy"}, 90),
    file_asset("style_ec8_ccs", "EC8A3572 风格描述", "style_lut", "styles/extracted/EC8A3572-DxO_DeepPRIME_3/style.ccs", "/styles/extracted/EC8A3572-DxO_DeepPRIME_3/style.ccs", {"format": "ccs"}, 100),
    file_asset("model_modflows_b0", "ModFlows B0 内置模型权重", "model_weight", "model_assets/modflows/modflows_color_encoder_B0_dim_515.pt", None, {"model": "modflows_b0"}, 110),
    file_asset("model_modflows_b6", "ModFlows B6 内置模型权重", "model_weight", "model_assets/modflows/modflows_color_encoder_B6_dim_8195_iter_700000.pt", None, {"model": "modflows_b6"}, 120),
]


def options(items: list[str]) -> list[dict]:
    return [{"key": chr(65 + index), "text": text} for index, text in enumerate(items)]


def selection(
    slug: str,
    category: str,
    difficulty: str,
    prompt: str,
    choices: list[str],
    answer_key: str,
    explanation: str,
    asset_slug: str | None = None,
) -> dict:
    return {
        "slug": slug,
        "category": category,
        "difficulty": difficulty,
        "prompt": prompt,
        "options_json": options(choices),
        "answer_key": answer_key,
        "explanation": explanation,
        "asset_slug": asset_slug,
        "is_active": 1,
    }


SELECTION_QUESTIONS = [
    selection("sel_color_001", "色彩基础", "easy", "追色前最应该先统一哪一项，避免算法把曝光差误判为风格差？", ["白平衡和曝光基准", "文件名长度", "画布缩放比例", "导出序号位数"], "A", "曝光和白平衡是追色的基线，基线不稳会污染后续色彩判断。"),
    selection("sel_color_002", "色彩基础", "easy", "Lab 空间中通常用哪两个通道描述颜色倾向？", ["a 与 b", "R 与 G", "X 与 Y", "Alpha 与 Mask"], "A", "Lab 的 L 表示亮度，a/b 表示绿红与蓝黄方向。"),
    selection("sel_color_003", "色彩基础", "medium", "目标图偏绿、参考图偏洋红时，追色主要会调整哪个方向？", ["a 轴方向", "锐化半径", "文件体积", "裁切比例"], "A", "绿色和洋红主要落在 Lab 的 a 轴对立方向上。"),
    selection("sel_color_004", "色彩基础", "easy", "判断追色结果是否自然，最先观察哪类区域更可靠？", ["中性灰和肤色", "纯黑边框", "文件扩展名", "水印位置"], "A", "中性灰和肤色对偏色最敏感，也最容易暴露不自然的迁移。"),
    selection("sel_color_005", "色彩基础", "medium", "参考图高光明显偏暖，目标图阴影偏冷，应该优先避免什么？", ["把高光色温强行套到所有阴影", "保存为 JPEG", "使用缩略图预览", "命名为 warm"], "A", "高光与阴影应分层处理，整体硬套容易产生脏色。"),
    selection("sel_lut_001", "LUT与预设", "easy", "内置暖调预设最适合用于哪类初筛？", ["快速建立偏暖色彩基调", "修复损坏文件", "删除项目", "识别登录用户"], "A", "暖调预设用于快速建立整体色温和情绪方向。", "profile_warm"),
    selection("sel_lut_002", "LUT与预设", "medium", "导入外部 LUT 前，最应该确认什么？", ["LUT 的色彩空间和强度是否匹配素材", "电脑桌面壁纸", "项目名称长度", "数据库端口"], "A", "LUT 与素材色彩空间不匹配时，结果很容易过饱和或偏色。"),
    selection("sel_lut_003", "LUT与预设", "easy", "黑白预设会主要削弱哪类信息？", ["色相和饱和度", "文件路径", "项目所有者", "图像尺寸"], "A", "黑白风格保留亮度结构，弱化或移除色相与饱和度。", "profile_bw"),
    selection("sel_lut_004", "LUT与预设", "medium", "同一 LUT 在不同曝光素材上差异明显，最合理的处理是？", ["先做曝光归一再应用 LUT", "重复上传同一文件", "关闭缩略图", "改成视频项目"], "A", "LUT 对输入亮度敏感，曝光归一能提高一致性。"),
    selection("sel_lut_005", "LUT与预设", "medium", "保存可复用风格预设时，应包含哪类信息？", ["LUT/调整参数与来源说明", "浏览器窗口位置", "鼠标移动轨迹", "服务器启动时间"], "A", "可复用预设需要保留实际调色参数和来源，便于复现。", "style_ec8_lut_cube"),
    selection("sel_portrait_001", "人像追色", "easy", "人像追色中最需要保护的区域通常是？", ["肤色中间调", "背景纯黑区", "文件边缘", "透明通道"], "A", "肤色中间调决定人像观感，过度迁移会显得不健康。"),
    selection("sel_portrait_002", "人像追色", "medium", "肤色已经自然但背景偏冷时，最佳策略是？", ["保护肤色，只追背景或全局弱化", "继续提高整体饱和度", "只看直方图峰值", "删除参考图"], "A", "肤色应作为保护区域，背景可单独调整。"),
    selection("sel_portrait_003", "人像追色", "medium", "面部高光被追成橙色块，通常说明什么？", ["高光区域迁移过强", "图片太小无法打开", "项目没有名字", "缩略图缓存失效"], "A", "高光缺少细节，过强迁移会形成色块。"),
    selection("sel_portrait_004", "人像追色", "easy", "商业人像调色中，肤色最忌讳哪种结果？", ["局部发灰或发绿", "文件名包含日期", "缩略图宽度较小", "使用 PNG 图标"], "A", "发灰发绿会直接破坏肤色可信度。"),
    selection("sel_portrait_005", "人像追色", "hard", "参考图肤色偏古铜而目标是冷白肤色，追色强度应如何控制？", ["降低肤色迁移强度，保留目标肤色身份", "完全套用参考肤色", "只调锐度", "只导出缩略图"], "A", "人像追色要保留主体身份，不宜强行改变肤色类型。"),
    selection("sel_region_001", "区域保护", "easy", "“保护点选区域”模式适合什么场景？", ["不希望局部被追色改变", "想删除所有图片", "需要重置密码", "要修改数据库名"], "A", "保护模式会降低指定区域受到的追色影响。"),
    selection("sel_region_002", "区域保护", "easy", "“只追点选区域”模式适合什么场景？", ["只想调整局部区域", "全局统一导出", "创建新账号", "关闭训练进度"], "A", "局部模式会把追色集中在点选区域。"),
    selection("sel_region_003", "区域保护", "medium", "天空和人物同时存在时，为避免肤色受天空蓝影响，应怎么做？", ["保护人物肤色或单独追天空", "提高全局蓝色饱和度", "把图片转成灰度", "删除参考区域"], "A", "天空色彩迁移容易污染肤色，区域隔离更稳。"),
    selection("sel_region_004", "区域保护", "medium", "区域 mask 边缘太硬会造成什么问题？", ["边界出现明显色彩断层", "数据库连接更快", "导出文件更小", "登录状态消失"], "A", "硬边界会让局部调色像贴片，需要羽化或软融合。"),
    selection("sel_region_005", "区域保护", "hard", "复杂前景发丝旁边的背景追色，最需要哪种处理？", ["细边缘 mask 与低强度融合", "强制矩形整块覆盖", "只调文件名", "关闭色彩管理"], "A", "发丝边缘需要细致 mask，否则会产生颜色溢出。"),
    selection("sel_hist_001", "亮度与直方图", "easy", "直方图匹配主要改变什么？", ["像素分布", "项目权限", "文件扩展名", "网络端口"], "A", "直方图匹配让目标的亮度或颜色分布接近参考。"),
    selection("sel_hist_002", "亮度与直方图", "medium", "参考图对比度很强，目标图很柔和，直接匹配可能带来什么？", ["阴影堵塞或高光过曝", "登录失败", "缩略图丢失", "路径变短"], "A", "强对比迁移到柔和图上容易压坏亮度层次。"),
    selection("sel_hist_003", "亮度与直方图", "medium", "亮度分区追色相比全局追色的优势是？", ["高光、中间调、阴影可分别迁移", "不需要读取图片", "自动创建用户", "只支持图标"], "A", "分区能避免一个亮度层的颜色强行影响另一个层。"),
    selection("sel_hist_004", "亮度与直方图", "easy", "评估暗部是否被压坏，应该看哪里？", ["阴影细节和噪声颜色", "项目列表标题", "按钮圆角", "数据库表名"], "A", "暗部细节和噪声色彩最能反映阴影处理是否过度。"),
    selection("sel_hist_005", "亮度与直方图", "hard", "参考图低饱和但高对比，目标图高饱和低对比，追色时应分开控制什么？", ["饱和度与明度曲线", "文件大小与文件名", "端口与域名", "账号与密码"], "A", "饱和度和明度对观感贡献不同，分开控制更可控。"),
    selection("sel_export_001", "文件与导出", "easy", "批量导出前最应该先确认哪项？", ["格式、尺寸、色彩空间", "鼠标速度", "浏览器缩放", "数据库编码名称"], "A", "导出参数决定交付质量和下游兼容性。"),
    selection("sel_export_002", "文件与导出", "easy", "需要保留透明通道时，优先选择哪种格式？", ["PNG", "JPEG", "TXT", "CUBE"], "A", "PNG 支持透明通道，JPEG 不支持。"),
    selection("sel_export_003", "文件与导出", "medium", "给网页使用的大量预览图，通常更应关注什么？", ["文件体积和视觉质量平衡", "训练 epoch 数", "数据库索引名", "项目拥有者邮箱"], "A", "网页预览需要加载速度与画质之间的平衡。"),
    selection("sel_export_004", "文件与导出", "medium", "导出到印刷流程前，应特别确认什么？", ["色彩空间和位深", "浏览器主题", "项目排序", "登录方式"], "A", "印刷流程对色彩空间和位深要求更严格。"),
    selection("sel_export_005", "文件与导出", "hard", "同一组图需要保持编号稳定，导出命名应避免什么？", ["依赖随机名称作为唯一顺序", "使用统一前缀", "固定序号位数", "保留原始名称片段"], "A", "随机名称不利于复核和交付顺序管理。"),
    selection("sel_train_001", "训练与模型", "easy", "训练 NeuralPreset 前，训练目录至少需要什么？", ["可读取的训练图片样本", "浏览器收藏夹", "空白文本文件", "导出日志截图"], "A", "模型训练需要有效图片样本。"),
    selection("sel_train_002", "训练与模型", "medium", "训练样本质量低、评分混杂，会主要影响什么？", ["模型风格稳定性", "登录按钮位置", "静态图标尺寸", "端口占用"], "A", "训练数据质量直接影响模型输出的一致性。"),
    selection("sel_train_003", "训练与模型", "medium", "归一化阶段权重缺失时，风格阶段通常应该怎样？", ["先补齐或训练归一化阶段", "跳过继续导出", "只改项目名称", "降低浏览器缩放"], "A", "风格阶段依赖归一化权重，缺失时应先补齐。"),
    selection("sel_train_004", "训练与模型", "hard", "训练损失下降但视觉效果变差，可能说明什么？", ["指标与目标审美不完全一致", "文件路径一定错误", "数据库没有表", "图片尺寸必然为 0"], "A", "训练指标只是代理目标，视觉评估仍然重要。"),
    selection("sel_train_005", "训练与模型", "easy", "后台显示模型权重缺失时，最直接的处理是？", ["把对应权重放入配置目录", "刷新文件名", "删除所有项目", "关闭 CORS"], "A", "模型推理需要实际权重文件可用。", "model_modflows_b0"),
    selection("sel_video_001", "视频追色", "easy", "视频追色相比单张图片，额外需要注意什么？", ["帧间一致性", "单张缩略图圆角", "登录验证码", "文件夹名字"], "A", "视频最怕帧间颜色跳变。"),
    selection("sel_video_002", "视频追色", "medium", "逐帧追色出现闪烁，通常应优先优化什么？", ["时间平滑和参考一致性", "按钮文案", "数据库密码", "图标大小"], "A", "视频需要时间维度上的稳定约束。"),
    selection("sel_video_003", "视频追色", "medium", "导出视频前，最应确认哪组参数？", ["格式、分辨率、帧率", "项目名称、邮箱、主题", "鼠标坐标、窗口大小、缩放", "索引名、表名、端口"], "A", "视频交付依赖格式、分辨率和帧率。"),
    selection("sel_video_004", "视频追色", "hard", "参考帧与目标镜头光照差异很大时，最稳妥的方式是？", ["降低强度并按镜头分段校正", "全片使用同一强 LUT", "只保留第一帧", "删除音轨"], "A", "光照差异大时需要分段和弱化，避免全片漂色。"),
    selection("sel_video_005", "视频追色", "easy", "视频批量处理进度卡住时，最应该查看什么？", ["任务日志和当前阶段", "logo 文件大小", "静态 CSS", "项目创建按钮"], "A", "任务日志能定位卡在读取、推理还是导出阶段。"),
    selection("sel_project_001", "项目管理", "easy", "将素材保存到项目中，最主要的好处是？", ["便于复盘追色过程和导出结果", "改变相机型号", "提升数据库端口速度", "自动增加显存"], "A", "项目保存能保留素材、结果和上下文。"),
    selection("sel_project_002", "项目管理", "medium", "多用户环境下，项目素材路径应优先满足什么？", ["用户隔离和访问控制", "路径越短越好", "所有人共享同目录", "文件名必须中文"], "A", "用户隔离能避免越权访问和数据混乱。"),
    selection("sel_project_003", "项目管理", "easy", "软删除项目的意义是？", ["隐藏项目但保留恢复可能", "立刻清空硬盘", "提升图片锐度", "修改图片色温"], "A", "软删除通常通过 deleted_at 标记，便于恢复或审计。"),
    selection("sel_project_004", "项目管理", "medium", "恢复历史工作区时，最关键的数据是？", ["workspace_snapshot", "按钮颜色", "浏览器标签标题", "临时端口"], "A", "工作区快照保存了当前编辑状态。"),
    selection("sel_project_005", "项目管理", "hard", "迁移旧素材路径时，最应避免什么？", ["破坏现有用户路径引用", "记录迁移日志", "保留回退信息", "按用户分目录"], "A", "迁移必须保护已有引用，否则项目会找不到素材。"),
    selection("sel_quality_001", "质量评估", "easy", "追色结果第一轮质检，最应该和什么对比？", ["目标原图与参考图", "数据库日志文件", "浏览器缓存", "项目 ID"], "A", "质检需要同时看原目标、参考和结果。"),
    selection("sel_quality_002", "质量评估", "medium", "结果过饱和时，最直接的调整方向是？", ["降低饱和度或追色强度", "提高序号位数", "修改用户角色", "删除样本目录"], "A", "过饱和通常由强度或色彩映射过大导致。"),
    selection("sel_quality_003", "质量评估", "medium", "结果看起来“脏”，常见原因是什么？", ["阴影色相迁移过强或白平衡不稳", "按钮太小", "项目名称为空", "端口不是 80"], "A", "阴影和白平衡问题很容易让画面发脏。"),
    selection("sel_quality_004", "质量评估", "hard", "算法指标高但客户不满意时，下一步更合理的是？", ["按客户参考和用途重新定义目标", "只增加数据库索引", "强制复用旧参数", "删除评价记录"], "A", "商业调色最终服务于用途和审美，指标不能替代需求。"),
    selection("sel_quality_005", "质量评估", "easy", "给结果打分的价值主要是？", ["沉淀高质量训练和复盘数据", "改变源文件格式", "缩短登录时间", "重启服务器"], "A", "评分能帮助筛选训练样本和持续改进。"),
]


AREA_GROUPS = [
    ("portrait_face_mid", "人像肤色", "半身人像", "脸颊到鼻翼附近的肤色中间调", [0.39, 0.23, 0.22, 0.20], "protect", "避开额头高光和嘴唇高饱和区域"),
    ("portrait_hand_skin", "人像肤色", "手持产品人像", "手背和手指的连续肤色区域", [0.31, 0.58, 0.24, 0.18], "protect", "避开指甲反光和产品边缘"),
    ("sky_upper", "天空", "户外风景", "天空上半部的干净蓝色区域", [0.05, 0.05, 0.90, 0.28], "local_only", "避开建筑边缘和树梢细节"),
    ("sky_sunset", "天空", "日落照片", "靠近地平线的暖色天空渐变", [0.10, 0.28, 0.80, 0.20], "local_only", "避开太阳本体和过曝云边"),
    ("shadow_corner", "阴影", "室内静物", "左下角可见细节的阴影区域", [0.05, 0.62, 0.30, 0.28], "local_only", "不要选纯黑无细节区域"),
    ("highlight_table", "高光", "餐桌场景", "盘子边缘的柔和高光", [0.55, 0.22, 0.22, 0.16], "protect", "避开完全过曝的白点"),
    ("product_body", "产品", "电商产品图", "产品主体中间调材质面", [0.34, 0.27, 0.32, 0.42], "local_only", "避开高光反射和背景"),
    ("product_label", "产品", "包装产品图", "包装正面的品牌标签", [0.40, 0.38, 0.22, 0.18], "protect", "保持文字和品牌色稳定"),
    ("greenery_mid", "植物", "城市绿植", "叶片中间调绿色区域", [0.12, 0.32, 0.34, 0.30], "local_only", "避开黄叶和深阴影"),
    ("water_mid", "水面", "海边场景", "水面中间调蓝绿色区域", [0.18, 0.48, 0.64, 0.24], "local_only", "避开白色浪花高光"),
]

AREA_VARIANTS = [
    ("基础圈选", "请标出{target}，用于{mode_text}。"),
    ("边缘避让", "在{scene}中选择{target}，同时注意{avoid}。"),
    ("追色参考", "如果只想让{category}接近参考图，应优先圈选哪里？"),
    ("保护判断", "{scene}追色前需要{mode_text}，请选择最合适的区域。"),
    ("质检定位", "结果偏色时，应该检查并圈出{target}作为局部复核区域。"),
]


def build_area_questions() -> list[dict]:
    rows = []
    index = 1
    for key, category, scene, target, rect, mode, avoid in AREA_GROUPS:
        for variant_index, (_variant, template) in enumerate(AREA_VARIANTS, start=1):
            mode_text = "保护该区域" if mode == "protect" else "只追该区域"
            dx = (variant_index - 3) * 0.008
            dy = ((variant_index % 3) - 1) * 0.006
            x, y, width, height = rect
            shifted = [
                round(max(0.0, min(0.98, x + dx)), 3),
                round(max(0.0, min(0.98, y + dy)), 3),
                round(width, 3),
                round(height, 3),
            ]
            prompt = template.format(category=category, scene=scene, target=target, mode_text=mode_text, avoid=avoid)
            rows.append(
                {
                    "slug": f"area_{index:03d}_{key}_{variant_index}",
                    "category": category,
                    "difficulty": "easy" if variant_index == 1 else ("medium" if variant_index in (2, 3, 4) else "hard"),
                    "prompt": prompt,
                    "image_asset_slug": "static_style_icon",
                    "target_area_json": {
                        "type": "rect",
                        "unit": "normalized",
                        "x": shifted[0],
                        "y": shifted[1],
                        "width": shifted[2],
                        "height": shifted[3],
                        "mode": mode,
                    },
                    "answer_json": {
                        "label": target,
                        "scene": scene,
                        "mode": mode,
                        "avoid": avoid,
                        "acceptance": "矩形覆盖主要区域即可，边缘可留 5%-10% 缓冲。",
                    },
                    "explanation": f"{target}能代表{category}的关键颜色，{avoid}。",
                    "is_active": 1,
                }
            )
            index += 1
    return rows


AREA_QUESTIONS = build_area_questions()

CREATE_TABLES = [
    """CREATE TABLE IF NOT EXISTS builtin_assets (
        id INT NOT NULL AUTO_INCREMENT,
        slug VARCHAR(96) NOT NULL,
        title VARCHAR(255) NOT NULL,
        asset_type VARCHAR(64) NOT NULL,
        file_path VARCHAR(1024) NOT NULL,
        public_url VARCHAR(1024) DEFAULT NULL,
        mime_type VARCHAR(128) DEFAULT NULL,
        size_bytes BIGINT NOT NULL DEFAULT 0,
        metadata LONGTEXT,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        sort_order INT NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY ux_builtin_assets_slug (slug),
        KEY ix_builtin_assets_type (asset_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS selection_questions (
        id INT NOT NULL AUTO_INCREMENT,
        slug VARCHAR(128) NOT NULL,
        category VARCHAR(64) NOT NULL,
        difficulty VARCHAR(32) NOT NULL DEFAULT 'medium',
        prompt TEXT NOT NULL,
        options_json LONGTEXT NOT NULL,
        answer_key VARCHAR(16) NOT NULL,
        explanation TEXT,
        asset_slug VARCHAR(96) DEFAULT NULL,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY ux_selection_questions_slug (slug),
        KEY ix_selection_questions_category (category)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    """CREATE TABLE IF NOT EXISTS area_questions (
        id INT NOT NULL AUTO_INCREMENT,
        slug VARCHAR(128) NOT NULL,
        category VARCHAR(64) NOT NULL,
        difficulty VARCHAR(32) NOT NULL DEFAULT 'medium',
        prompt TEXT NOT NULL,
        image_asset_slug VARCHAR(96) DEFAULT NULL,
        target_area_json LONGTEXT NOT NULL,
        answer_json LONGTEXT NOT NULL,
        explanation TEXT,
        is_active TINYINT(1) NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY ux_area_questions_slug (slug),
        KEY ix_area_questions_category (category)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
]


async def upsert_many(cur, table: str, columns: list[str], rows: list[dict], json_columns: set[str] | None = None) -> None:
    json_columns = json_columns or set()
    placeholders = ", ".join(["%s"] * len(columns))
    updates = ", ".join(f"`{column}`=VALUES(`{column}`)" for column in columns if column != "slug")
    column_names = ", ".join(f"`{column}`" for column in columns)
    sql = f"INSERT INTO `{table}` ({column_names}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
    values = []
    for row in rows:
        item = []
        for column in columns:
            value = row[column]
            if column in json_columns:
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            item.append(value)
        values.append(tuple(item))
    await cur.executemany(sql, values)


async def seed_assets_table(cur) -> int:
    inserted = 0
    for asset in BUILTIN_ASSETS:
        file_name = asset["file_path"][:512]
        await cur.execute("SELECT id FROM assets WHERE project_id IS NULL AND file_name=%s LIMIT 1", (file_name,))
        if await cur.fetchone():
            continue
        await cur.execute("INSERT INTO assets (project_id, file_name, rating) VALUES (NULL, %s, %s)", (file_name, 0))
        inserted += 1
    return inserted


async def main() -> None:
    load_local_env()
    if len(SELECTION_QUESTIONS) != 50:
        raise RuntimeError(f"selection question count mismatch: {len(SELECTION_QUESTIONS)}")
    if len(AREA_QUESTIONS) != 50:
        raise RuntimeError(f"area question count mismatch: {len(AREA_QUESTIONS)}")

    conn = await aiomysql.connect(**mysql_config())
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            for ddl in CREATE_TABLES:
                await cur.execute(ddl)
            await upsert_many(
                cur,
                "builtin_assets",
                ["slug", "title", "asset_type", "file_path", "public_url", "mime_type", "size_bytes", "metadata", "is_active", "sort_order"],
                BUILTIN_ASSETS,
                {"metadata"},
            )
            inserted_assets = await seed_assets_table(cur)
            await upsert_many(
                cur,
                "selection_questions",
                ["slug", "category", "difficulty", "prompt", "options_json", "answer_key", "explanation", "asset_slug", "is_active"],
                SELECTION_QUESTIONS,
                {"options_json"},
            )
            await upsert_many(
                cur,
                "area_questions",
                ["slug", "category", "difficulty", "prompt", "image_asset_slug", "target_area_json", "answer_json", "explanation", "is_active"],
                AREA_QUESTIONS,
                {"target_area_json", "answer_json"},
            )
            await conn.commit()

            print(f"assets_table_inserted={inserted_assets}")
            for table in ("assets", "builtin_assets", "selection_questions", "area_questions"):
                await cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
                print(f"{table}={(await cur.fetchone())['n']}")
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
