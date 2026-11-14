# -*- coding: utf-8 -*-
"""萌萌今日老婆 · 立绘提示词生成（变化维度池 + 可选 LLM 通道）。

背景（v0.5.6）：图生图主用 Qwen-Image-2.1（通义千问图像模型，文生图与图像
编辑统一在同一模型里，编辑由自然语言指令驱动）。其提示词是「自然语言编辑指令」
而非 danbooru tag——写法要点（官方指南）：动作 + 目标 + 约束，一句话里说清
「改什么」并强调「什么保持不变」，否则结果漂移。

两个通道：
- LLM 通道（默认开）：把角色信息 + 本轮变化维度（种子决定）交给所配对话模型，
  产一条 Qwen-Image-Edit 编辑指令；失败/超时 → 本地模板兜底，绝不阻塞出卡。
- 本地模板：无 astrbot 依赖的确定性拼装（可独立测试）。

变化维度（artstyle/outfit/background/pose/expression/body/compose）由种子决定论
派生：同角色同天第 seq 张恒定（重试不换装）。body 维度大权重保持原样，
小概率触发身高/体型/胸部变化（用户明确要的「萝莉变高挑」一类）。
v0.19.0 起新增 artstyle（画风）维度——此前画风完全由底模决定，换主题只换
衣服与背景，一组立绘看起来像同一套流程的复制品。
"""
from __future__ import annotations

import asyncio
import hashlib
import re

try:  # AstrBot 运行期
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地平铺调试
    import logging

    logger = logging.getLogger("moe_waifu")

# ---------- 变化维度池 ----------
# 每项 {"w": 权重, "zh": 中文短语（编辑指令用）, "en": 英文 tag（文生图用）}
# zh 为空 = 无需提及该项；en 为空 = 文生图不追加 tag。

OUTFITS = [
    # —— JK / 校园系（campus）——
    {"w": 5, "zh": "水手服JK制服", "en": "sailor school uniform, serafuku"},
    # v0.13.0：JK / 校园系扩充（用户反馈「校园主题每次都是水手服」：
    # 原来主题子集里只有水手服(权重9)与运动外套(权重5)，等于二选一）
    {"w": 5, "zh": "西装夹克JK制服（格纹百褶裙）", "en": "blazer, school uniform, plaid skirt"},
    {"w": 5, "zh": "针织开衫JK制服（衬衫领结 + 百褶裙）", "en": "cardigan, school uniform, shirt, ribbon, pleated skirt"},
    {"w": 4, "zh": "背带裙JK制服（白衬衫 + 背带）", "en": "suspender skirt, school uniform, shirt"},
    {"w": 4, "zh": "马甲衬衫JK制服", "en": "vest, shirt, school uniform"},
    {"w": 4, "zh": "长袖白衬衫与深色百褶裙（学院风）", "en": "white shirt, pleated skirt, school uniform"},
    {"w": 3, "zh": "冬季呢子校服大衣与围巾", "en": "duffle coat, school uniform, scarf"},
    {"w": 3, "zh": "校园体操服", "en": "gym uniform, school sportswear"},
    {"w": 3, "zh": "拉拉队服", "en": "cheerleader outfit"},
    # —— 婚纱 / 礼服（wedding）——
    {"w": 7, "zh": "婚纱", "en": "wedding dress, bridal veil"},
    {"w": 4, "zh": "公主蓬蓬婚纱（拖尾与头纱）", "en": "ball gown wedding dress, bridal veil"},
    {"w": 3, "zh": "鱼尾婚纱", "en": "mermaid wedding dress"},
    {"w": 4, "zh": "吊带礼服", "en": "evening gown, sleeveless dress"},
    {"w": 4, "zh": "抹胸晚礼服", "en": "strapless evening gown"},
    {"w": 3, "zh": "短款派对小礼服", "en": "short party dress"},
    {"w": 4, "zh": "秀禾服", "en": "chinese wedding dress, hanfu"},
    {"w": 3, "zh": "花嫁和服（白无垢）", "en": "shiromuku, wedding kimono"},
    # v0.13.1：旗袍不再写死黑色，颜色交给 OUTFIT_COLORS；款式按常见变体扩充
    {"w": 6, "zh": "旗袍", "en": "qipao, china dress"},
    {"w": 4, "zh": "改良短款旗袍", "en": "short qipao, china dress"},
    {"w": 4, "zh": "缎面提花旗袍", "en": "satin qipao, china dress"},
    {"w": 4, "zh": "蕾丝无袖旗袍", "en": "lace qipao, sleeveless, china dress"},
    {"w": 3, "zh": "长款修身旗袍（侧开叉）", "en": "long qipao, side slit, china dress"},
    {"w": 3, "zh": "鱼尾摆改良旗袍", "en": "mermaid qipao, china dress"},
    {"w": 3, "zh": "立领盘扣旗袍（双层袖口）", "en": "qipao, mandarin collar, china dress"},
    {"w": 3, "zh": "旗袍配毛披肩", "en": "qipao, fur shawl"},
    # —— 泳装系（swimsuit）。v0.14.0 大扩充：原子集只有 3 项、连体泳装独占
    #    46% 权重，且「连体竞技泳衣」因叫「泳衣」漏出主题关键词——用户反馈
    #    「每次换一版都是连体泳衣，比基尼也算啊」——
    {"w": 5, "zh": "连体泳装", "en": "one-piece swimsuit"},
    {"w": 5, "zh": "分体比基尼", "en": "bikini"},
    {"w": 4, "zh": "荷叶边比基尼", "en": "frilled bikini"},
    {"w": 4, "zh": "运动系比基尼", "en": "sport bikini"},
    {"w": 3, "zh": "系带比基尼", "en": "side-tie bikini"},
    {"w": 4, "zh": "连体裙式泳衣", "en": "one-piece swimsuit, swim skirt"},
    {"w": 4, "zh": "连体竞技泳衣", "en": "competition swimsuit, one-piece swimsuit"},
    {"w": 3, "zh": "长袖防晒泳衣", "en": "rash guard, one-piece swimsuit"},
    {"w": 3, "zh": "泳装配纱笼罩裙", "en": "swimsuit, sarong"},
    {"w": 3, "zh": "泳装外搭防晒衬衫", "en": "swimsuit, oversized shirt"},
    # —— 职场 / 通勤（office）。v0.14.0 关键词精确化：原来「西装」「马甲」
    #    会把 JK 制服吸进职场子集，改用「职业西装/通勤/铅笔裙」等专属词——
    {"w": 7, "zh": "干练的职业西装套裙", "en": "business suit, office lady"},
    {"w": 4, "zh": "西装裤装套装", "en": "business suit, pants suit"},
    {"w": 3, "zh": "马甲与衬衫的通勤装", "en": "vest, shirt, office lady"},
    {"w": 4, "zh": "白衬衫配铅笔裙", "en": "white shirt, pencil skirt"},
    {"w": 4, "zh": "高领毛衣配长裤（通勤）", "en": "turtleneck sweater, dress pants"},
    {"w": 4, "zh": "风衣通勤装", "en": "trench coat, office lady"},
    {"w": 3, "zh": "条纹衬衫配西裤", "en": "striped shirt, dress pants"},
    {"w": 3, "zh": "航空制服（空姐装）", "en": "flight attendant uniform"},
    # —— 和风（kimono）——
    {"w": 6, "zh": "日式浴衣", "en": "yukata, japanese clothes"},
    {"w": 4, "zh": "振袖和服（深色系）", "en": "furisode, japanese clothes"},
    {"w": 4, "zh": "振袖和服（浅色系）", "en": "furisode, light colored kimono"},
    {"w": 4, "zh": "访问着和服", "en": "homongi, kimono"},
    {"w": 3, "zh": "舞妓风和服", "en": "maiko style kimono"},
    {"w": 4, "zh": "巫女装束（袴）", "en": "miko outfit, hakama"},
    # —— 女仆（maid）。v0.14.0 前没有任何主题入口，只有随机模式能抽到——
    {"w": 6, "zh": "女仆装", "en": "maid outfit, maid headdress"},
    {"w": 4, "zh": "咖啡厅女仆装（围裙 + 发带）", "en": "maid outfit, apron, maid headdress"},
    {"w": 3, "zh": "和风女仆装", "en": "wa maid, japanese clothes, maid outfit"},
    {"w": 3, "zh": "维多利亚风长裙女仆装", "en": "victorian maid, long dress, apron"},
    {"w": 3, "zh": "短袖夏装女仆装", "en": "maid outfit, short sleeves, apron"},
    # —— 哥特 / 暗黑（gothic）——
    {"w": 5, "zh": "哥特萝莉装", "en": "gothic lolita dress, frills"},
    {"w": 4, "zh": "女巫斗篷与尖顶帽", "en": "witch hat, cape, mage outfit"},
    {"w": 4, "zh": "哥特洋装（黑白蕾丝）", "en": "gothic dress, black and white lace"},
    {"w": 3, "zh": "哥特朋克裙（皮带与链条）", "en": "gothic punk dress, belts, chains"},
    {"w": 3, "zh": "吸血鬼风立领长裙", "en": "vampire dress, high collar"},
    {"w": 3, "zh": "魔法学院长袍", "en": "academy robe, mage outfit"},
    # —— 居家 / 慵懒（home）——
    {"w": 4, "zh": "宽松居家睡衣", "en": "pajamas, loungewear"},
    {"w": 4, "zh": "珊瑚绒睡袍", "en": "bathrobe, fluffy loungewear"},
    {"w": 4, "zh": "吊带睡裙", "en": "nightgown, camisole"},
    {"w": 4, "zh": "宽松T恤与居家短裤", "en": "oversized t-shirt, shorts"},
    {"w": 4, "zh": "连帽卫衣与居家裤", "en": "hoodie, sweatpants"},
    {"w": 3, "zh": "动物造型的连体家居服", "en": "animal kigurumi, onesie"},
    # —— 偶像 / 舞台（idol）。v0.14.0 前子集只有打歌服 1 项——
    {"w": 5, "zh": "演唱会打歌服", "en": "idol costume, stage outfit"},
    {"w": 4, "zh": "蝴蝶结舞台连衣裙", "en": "idol costume, ribbon dress"},
    {"w": 4, "zh": "亮片演出服", "en": "sequined stage outfit"},
    {"w": 3, "zh": "帅气风打歌服（领结与短裤）", "en": "idol costume, bow tie, shorts"},
    {"w": 3, "zh": "学园偶像演出服", "en": "school idol costume"},
    # —— v0.37.0 穿搭表现力扩充（用户参考图拆解：参考好图的穿搭是「层叠
    #    混搭、有细节有态度」——滑落的开衫、腿环、多重腰带、露腰层叠；原池
    #    74 条都是规矩成衣，换装换来换去就那几样）。五路补充，zh 关键词
    #    顺带挂接主题子集（卫衣→home、泳装/比基尼→swimsuit、哥特→gothic、
    #    毛衣→home/office）：
    # 街头混搭系
    {"w": 4, "zh": "露脐短上衣与工装短裤（腿环）",
     "en": "crop top, cargo shorts, thigh strap, midriff, navel"},
    {"w": 4, "zh": "oversize 连帽卫衣滑落单肩（内搭吊带）",
     "en": "oversized hoodie, falling off one shoulder, camisole, collarbone"},
    {"w": 3, "zh": "Y2K 短上衣与低腰牛仔裤",
     "en": "y2k fashion, crop top, low-rise jeans, midriff"},
    {"w": 3, "zh": "皮夹克与百褶短裙", "en": "leather jacket, pleated skirt"},
    {"w": 3, "zh": "白衬衫下摆系结与牛仔短裤",
     "en": "knotted shirt, midriff, denim shorts"},
    {"w": 4, "zh": "针织开衫滑落露单肩（内搭吊带裙）",
     "en": "cardigan falling off one shoulder, slip dress, collarbone"},
    {"w": 3, "zh": "西装外套披在肩上（内搭吊带裙）",
     "en": "blazer draped over shoulders, slip dress"},
    {"w": 3, "zh": "挂脖露脐上衣与宽腿裤",
     "en": "halterneck, midriff, navel, wide-leg pants"},
    {"w": 3, "zh": "吊带连衣裙叠穿长袖T恤",
     "en": "slip dress over t-shirt, layered clothing"},
    # 泳装混搭（进泳装主题子集；「开衫」同时触发外套维度自洽沉默）
    {"w": 3, "zh": "泳装混搭风（比基尼上装、羽毛短裙与条纹臂套）",
     "en": "bikini top, feather skirt, striped arm warmers, jewelry"},
    {"w": 3, "zh": "泳装外罩宽松防晒开衫（敞开未扣）",
     "en": "swimsuit, open cardigan, beach cover up, swimsuit under clothes"},
    # 异域华丽系（对齐「黑金埃及风」类参考）
    {"w": 3, "zh": "异域神明风（黑金短外套、挂脖露脐上衣与多重腰带金饰）",
     "en": "open jacket, gold trim, halterneck, multiple belts, gold accessories, navel, jewelry"},
    {"w": 3, "zh": "部族风舞衣（银饰、流苏与露腰）",
     "en": "tribal outfit, silver jewelry, tassel, midriff, navel"},
    # 慵懒滑肩系（进居家主题子集；毛衣同时进职场子集）
    {"w": 4, "zh": "oversize 毛衣滑落露肩与格纹裙",
     "en": "off-shoulder sweater, falling off one shoulder, plaid skirt, collarbone"},
    # 哥特扩展（进哥特主题子集）
    {"w": 3, "zh": "哥特束腰腰封与薄纱层叠裙",
     "en": "gothic, corset, waist cincher, sheer skirt, layered skirt"},
    # —— 通用（无主题归属，随机模式专属）——
    {"w": 5, "zh": "运动外套与短裤", "en": "track jacket, gym clothes"},
    {"w": 5, "zh": "冬季大衣与围巾", "en": "winter coat, scarf"},
    {"w": 6, "zh": "碎花夏日连衣裙", "en": "floral summer dress"},
    # 以下单条同时被多个主题共享：office（毛衣）与 home（毛衣）
    {"w": 5, "zh": "宽松针织毛衣与长裙", "en": "oversized sweater, long skirt"},
]

BACKGROUNDS = [
    # v0.36.0：影棚纯色底整体降权（此前 w=8/7/6 合计占比偏高，「干净但空」
    # 是产出同质化的推手之一）——保留给证价/头像类场景用，随机里让位给
    # 有光线、有生活的场景。
    {"w": 4, "zh": "纯白色摄影棚背景（柔光箱打光）", "en": "simple background, white background, studio lighting"},
    {"w": 4, "zh": "浅粉色纯色背景", "en": "simple background, pink background"},
    {"w": 3, "zh": "浅灰渐变摄影棚背景", "en": "simple background, grey background, soft lighting"},
    {"w": 6, "zh": "樱花树下的春日午后（花瓣飘落逆光透亮）", "en": "cherry blossoms, outdoors, sunlight, falling petals, backlighting"},
    {"w": 6, "zh": "教室窗边（午后斜阳穿过百叶、光斑落在课桌上）", "en": "classroom, window, afternoon sunlight through blinds, light rays, dust particles"},
    {"w": 5, "zh": "黄昏时分的街道（金色夕阳把影子拉得很长）", "en": "sunset, street, outdoors, golden hour, long shadows"},
    {"w": 5, "zh": "星空下的夜晚屋顶（城市灯火在远处铺开）", "en": "night, stars, rooftop, city lights, night sky"},
    {"w": 5, "zh": "夏日海边（蓝天白云、海面波光粼粼）", "en": "beach, ocean, summer, blue sky, sparkling water, sunlight"},
    {"w": 5, "zh": "霓虹闪烁的都市夜景", "en": "city lights, night, neon lights"},
    {"w": 5, "zh": "绿意花园（树影间洒落的阳光）", "en": "garden, flowers, dappled sunlight"},
    {"w": 4, "zh": "雨天的窗边（室内）", "en": "rain, window, indoors"},
    {"w": 5, "zh": "暖色调的咖啡馆内景", "en": "cafe, indoors, warm lighting"},
    {"w": 3, "zh": "咖啡店吧台（蒸汽氤氲）", "en": "coffee shop counter, espresso machine, steam"},
    {"w": 4, "zh": "传统和室榻榻米", "en": "japanese room, tatami"},
    {"w": 4, "zh": "圣诞夜的彩灯街道", "en": "christmas lights, night, festive"},
    # —— v0.14.0 扩充：此前泳装/校园/职场等主题的背景子集只有 1~3 个，
    #    「泳装永远海边」即源于此。按主题成组补充，命名自带主题关键词——
    # 泳装系（海边 / 泳池 / 水上 / 度假）
    {"w": 5, "zh": "泳池边（躺椅与遮阳伞）", "en": "poolside, beach chair, parasol"},
    {"w": 4, "zh": "水上乐园（滑梯与浪花）", "en": "water park, water slide, splashing"},
    {"w": 4, "zh": "海边黄昏（橙色夕阳下的海面）", "en": "beach, sunset, orange sky, ocean"},
    {"w": 3, "zh": "夏日室内泳池（玻璃穹顶洒光）", "en": "indoor pool, glass ceiling"},
    {"w": 4, "zh": "度假酒店阳台远眺海面", "en": "resort balcony, ocean view"},
    # 校园系（天台 / 走廊 / 图书馆 / 体育馆）
    {"w": 4, "zh": "学校天台（围栏与蓝天）", "en": "school rooftop, fence, blue sky"},
    {"w": 4, "zh": "放学后的走廊（夕阳斜照）", "en": "school hallway, sunset light"},
    {"w": 3, "zh": "图书馆书架之间", "en": "library, bookshelves"},
    {"w": 3, "zh": "室内体育馆", "en": "gymnasium, indoors"},
    # 婚礼系（教堂 / 拱门 / 宴会）
    {"w": 5, "zh": "教堂内（彩窗透光）", "en": "church interior, stained glass, light rays"},
    {"w": 4, "zh": "白花拱门下的婚礼花园", "en": "wedding garden, white flower arch"},
    {"w": 3, "zh": "宴会厅水晶吊灯下", "en": "banquet hall, chandelier"},
    # 职场系（办公室 / 写字楼 / 天桥）
    {"w": 5, "zh": "现代办公室（落地窗外是城市）", "en": "modern office, large window, city view"},
    {"w": 3, "zh": "写字楼大堂（玻璃幕墙）", "en": "office lobby, glass wall"},
    {"w": 3, "zh": "傍晚的城市天桥", "en": "city footbridge, dusk"},
    # 和风系（祭典 / 温泉 / 枯山水）
    {"w": 5, "zh": "夏日祭典夜晚（灯笼与摊位）", "en": "summer festival, lanterns, food stalls, night"},
    {"w": 4, "zh": "温泉旅馆回廊", "en": "hot spring inn, wooden corridor"},
    {"w": 3, "zh": "枯山水庭园", "en": "zen garden, rock garden, japanese garden"},
    # 哥特系（古堡 / 烛光 / 墓园）
    {"w": 4, "zh": "月光下的古堡阳台", "en": "moonlight, castle balcony, night"},
    {"w": 4, "zh": "烛光照亮的暗室书房", "en": "candlelight, dark study room"},
    {"w": 3, "zh": "迷雾笼罩的墓园小径", "en": "foggy graveyard, path, mist"},
    # 居家系（客厅 / 卧室）
    {"w": 4, "zh": "洒满阳光的客厅沙发", "en": "living room, sofa, sunlight through window"},
    {"w": 3, "zh": "窗边沙发座（绿植与午后阳光）", "en": "window side sofa, plants, afternoon light"},
    {"w": 3, "zh": "铺着地毯的卧室地板", "en": "bedroom, carpet, cozy"},
    # 偶像系（舞台 / 直播间）
    {"w": 6, "zh": "演唱会舞台（追光灯与荧光棒人海）", "en": "concert stage, spotlight, penlight crowd"},
    {"w": 3, "zh": "电台直播间", "en": "radio studio, microphone"},
    # 东方系（中式庭院 / 红灯笼）
    {"w": 4, "zh": "中式庭院（红墙与月门）", "en": "chinese garden, red wall, moon gate"},
    {"w": 3, "zh": "挂满红灯笼的夜市", "en": "red lanterns, night market"},
    # —— v0.36.0 表现力升级（用户参考图拆解）：此前背景池本质是「地点名单」，
    #    参考好图里真正卖钱的是**光**（逆光叶隙、金色黄昏、光斑粒子——参考图
    #    「树下吃西瓜」「秋日银杏」的主角其实是光）与**叙事**（物件堆满的房间、
    #    「正在做某事」的瞬间感）与**图形化构成**（网点+色块+斜纹）。按四路扩池：
    # 光线驱动：光就是画面的主角（条目可选带 light 字段 = qwen 本地兜底的光照句）
    {"w": 7, "zh": "树下乘凉（叶隙强逆光、光斑洒满全身，空气里浮着微尘光点）",
     "en": "under tree shade, dappled sunlight, backlighting, sun, light particles, lens flare",
     "light": "叶隙漏下的强逆光在轮廓上镶出金边，斑驳光斑洒在皮肤与衣物上，空气里有浮动的微尘光点"},
    {"w": 6, "zh": "金色黄昏的原野（夕阳把轮廓镀成金边）",
     "en": "golden hour, field, sunset, backlighting, rim light, warm colors",
     "light": "低角度的暖金色夕阳逆光，人物轮廓镶着金边、发丝半透明发亮，整体暖调"},
    {"w": 5, "zh": "晨光透过薄纱窗帘的房间（通体透亮的柔逆光）",
     "en": "indoors, sheer curtains, morning light, backlighting, soft light, airy",
     "light": "晨光透过薄纱窗帘变成通体透亮的柔逆光，亮部轻盈、阴影淡而软"},
    {"w": 5, "zh": "秋日银杏街道（满地落叶、斑驳树影落在身上）",
     "en": "autumn, ginkgo leaves, fallen leaves, dappled sunlight, street, warm colors",
     "light": "秋日斜阳穿过枝叶，斑驳的光影碎块落在肩头与地面，暖金与青蓝对比"},
    {"w": 4, "zh": "雨后的街角（湿漉漉的路面倒映着霓虹）",
     "en": "after rain, wet street, reflections, puddle, neon lights, evening",
     "light": "雨后傍晚，湿路面的镜面倒影裹着霓虹光，环境冷调、点缀高饱和光色"},
    {"w": 4, "zh": "雪后初晴（雪面反光透着淡粉天色）",
     "en": "snow, winter, reflective snow, sunlight, blue sky, pale pink sky",
     "light": "雪面把阳光反射成大面积柔光，天际一抹淡粉，整体清透微冷调"},
    {"w": 4, "zh": "午后天台（晾晒的床单被风掀起、透光的布料）",
     "en": "rooftop, laundry, backlit fabric, blue sky, wind, sunlight",
     "light": "午后天台，逆光里半透明的床单被风吹起，云影在天台上缓缓移动"},
    {"w": 3, "zh": "夏夜庭院（风铃轻响、远处有零星烟火的光）",
     "en": "summer night, garden, wind chime, fireworks in distance, night sky",
     "light": "夏夜暗青色调，远处烟火与檐下灯笼提供点状暖光源，人物被夜色包裹"},
    {"w": 3, "zh": "萤火虫漂浮的夏夜河堤",
     "en": "riverbank, night, fireflies, glowing particles, dark blue",
     "light": "深蓝夜色里萤火虫的绿色光点点缀，月亮提供柔和冷光，水面泛微光"},
    {"w": 3, "zh": "车窗边（流动的光斑掠过脸侧）",
     "en": "inside train, window seat, motion blur, passing scenery, sunlight",
     "light": "行进的车厢里，窗外风景拉成流动的光斑扫过脸侧与窗框，明暗交替"},
    # 物件叙事：环境里堆满角色的生活痕迹（参考图「粉色卫衣房间」那类的骨架）
    {"w": 5, "zh": "塞满收藏的房间（唱片机、海报、玩偶与零食袋）",
     "en": "cozy room, record player, posters, plush toys, snacks, warm lighting, cluttered"},
    {"w": 4, "zh": "深夜便利店门口（冷白灯光与关东煮的蒸汽）",
     "en": "convenience store, night, fluorescent light, steam, street",
     "light": "店内冷白荧光灯光泻到人行道上，与夜的暖色路灯光形成冷暖对比"},
    {"w": 4, "zh": "台灯下的书桌学习角（摊开的笔记与冒着热气的可可）",
     "en": "study desk, lamp light, open notebook, hot cocoa, night, cozy",
     "light": "台灯的暖黄光罩住书桌一角，四周渐渐没入夜色，光域之外发虚"},
    {"w": 4, "zh": "飘着烘焙香气的厨房（面粉与刚出炉的饼干）",
     "en": "kitchen, baking, flour, cookies, oven, warm light, cozy"},
    {"w": 3, "zh": "铺满靠垫的飘窗读书角（薄毯与散落的书）",
     "en": "bay window, cushions, books, blanket, soft daylight, cozy"},
    # 图形化背景：海报式构成（对齐「撞色色块+半调网点」那类参考图）
    {"w": 4, "zh": "撞色几何背景（大色块、半调网点与斜切条纹）",
     "en": "halftone, color blocks, diagonal stripes, graphic background, pop art, flat colors"},
    {"w": 3, "zh": "放射集中线背景（速度感与冲击力）",
     "en": "speed lines, radial background, manga effects, dynamic, flat colors"},
    {"w": 3, "zh": "同色系渐变背景加漂浮符号（爱心、星星与气泡）",
     "en": "gradient background, floating hearts, stars, bubbles, decorative, flat colors"},
    {"w": 2, "zh": "报纸拼贴背景（旧印刷质感与裁剪字迹）",
     "en": "newspaper collage, printed text, paper texture, vintage, flat background"},
    # 装饰拼贴（doujin 封面风：波点边框、贴纸、颜文字——低权重实验位）
    {"w": 2, "zh": "带装饰相框的封面构图（波点边框、贴纸与颜文字气泡）",
     "en": "decorated frame, polka dot border, stickers, kaomoji, scrapbook, colorful"},
    {"w": 2, "zh": "拍立得照片与胶带叠贴的背景",
     "en": "polaroid photos, scattered photos, washi tape, flat lay, pastel"},
]

POSES = [
    {"w": 6, "zh": "双手在胸前比出爱心", "en": "heart hands"},
    {"w": 5, "zh": "回头看向镜头", "en": "looking back, looking at viewer"},
    {"w": 5, "zh": "双臂抱胸、自信地站立", "en": "arms crossed, confident pose"},
    {"w": 5, "zh": "单手撩动头发", "en": "hand in own hair"},
    {"w": 5, "zh": "抬手挥手打招呼", "en": "waving"},
    {"w": 4, "zh": "双手捧住脸颊", "en": "hands on own cheeks"},
    {"w": 4, "zh": "怀里抱着毛绒玩偶", "en": "holding plush toy"},
    {"w": 4, "zh": "手里拿着一杯奶茶", "en": "holding cup, holding drink"},
    {"w": 4, "zh": "双手背在身后、身体微微前倾", "en": "arms behind back, leaning forward"},
    {"w": 4, "zh": "侧过身闭眼感受微风", "en": "side view, closed eyes, wind"},
    # —— v0.7.0：补全身姿势（zh 里写明腿/脚的动作，img2img 编辑指令才能带动下半身，
    #    修「腿和脚几乎不变、姿势永远一样」）——
    {"w": 6, "zh": "自然站立，双脚并拢微微内八", "en": "standing, full body, feet together"},
    {"w": 5, "zh": "盘腿坐在地上", "en": "sitting, cross-legged, on floor, full body"},
    {"w": 5, "zh": "坐在椅子上、双腿并拢斜放", "en": "sitting on chair, legs together, full body"},
    {"w": 4, "zh": "蹲下抱住膝盖", "en": "squatting, hugging own knees, full body"},
    {"w": 4, "zh": "侧躺在地上、一只手撑着头", "en": "lying on side, on floor, head in hand, full body"},
    {"w": 3, "zh": "仰面躺着、双腿翘起", "en": "lying on back, on floor, legs raised, full body"},
    {"w": 4, "zh": "单腿站立、另一条腿屈膝抬起", "en": "standing on one leg, leg raised, balancing, full body"},
    {"w": 4, "zh": "原地轻跳、双脚离地", "en": "jumping, feet off ground, full body"},
    {"w": 4, "zh": "迈步走路、裙摆飘动", "en": "walking, full body, dynamic pose"},
    # v0.19.10：趴桌/蜷缩睡这两条去掉 "full body"——趴在桌上不可能同时呈现
    # 「全身站立像」，en tag 自相矛盾正是双头事故的推手之一（模型把睡着的头
    # 画在桌上、又把原姿势的全身人画了一遍）。
    {"w": 4, "zh": "趴在桌上打瞌睡", "en": "sleeping on desk, dozing off"},
    {"w": 4, "zh": "抱着枕头蜷缩打瞌睡", "en": "sleeping, hugging pillow, curled up"},
    {"w": 4, "zh": "弯腰凑近镜头做搞怪鬼脸", "en": "bending forward, playful pose, funny face"},
    {"w": 3, "zh": "伸展双臂伸懒腰", "en": "stretching, arms up, full body"},
    {"w": 3, "zh": "背对镜头回眸", "en": "from behind, looking back at viewer, full body"},
    # —— v0.14.0 扩充：日常小动作系（全局换新感）——
    {"w": 4, "zh": "双手比出剪刀手", "en": "v sign, peace sign"},
    {"w": 4, "zh": "单手叉腰自信站立", "en": "hand on hip, standing, confident"},
    {"w": 4, "zh": "手里举着冰淇淋", "en": "holding ice cream cone"},
    {"w": 3, "zh": "撑着伞回头", "en": "holding umbrella, looking back"},
    {"w": 3, "zh": "坐在窗台上双腿悬空", "en": "sitting on windowsill, legs dangling"},
    # —— v0.36.0 生活瞬间系（用户参考图拆解）：「正在做某件事」的抓拍感 +
    #    道具互动——参考图里最动人的是「穿高跟鞋的瞬间」「吃西瓜」「捧着杯子
    #    喝咖啡」这类动作，而不是站桩摆拍。镜头语言（低角度 / 动势）直接写进
    #    个别条目，不新增维度。
    {"w": 4, "zh": "捧着冰镇西瓜正要咬下去（夏日抓拍）",
     "en": "eating watermelon, holding watermelon slice, juicy"},
    {"w": 4, "zh": "双手捧着马克杯喝热饮、低头轻轻吹气",
     "en": "drinking from mug, holding cup, holding warm drink, steam"},
    {"w": 4, "zh": "单脚踩在凳子上正要穿鞋",
     "en": "putting on shoes, one foot on stool, leaning forward, dynamic pose"},
    {"w": 4, "zh": "趴在地毯上翘起双腿玩掌上游戏机",
     "en": "lying on stomach, on carpet, playing handheld game console, kicked up feet"},
    {"w": 4, "zh": "戴上头戴式耳机闭眼跟着音乐轻轻摇头",
     "en": "headphones around neck, listening to music, closed eyes, head tilted"},
    {"w": 4, "zh": "抱着快递纸箱小跑过来",
     "en": "carrying cardboard box, running, dynamic pose, leaning forward"},
    {"w": 4, "zh": "咬着吐司边冲出家门（迟到抓拍）",
     "en": "toast in mouth, running, dynamic pose, late for school"},
    {"w": 3, "zh": "倚着墙低头专注刷手机",
     "en": "leaning against wall, looking at phone, smartphone"},
    {"w": 3, "zh": "转着伞踮脚跳过积水的水洼",
     "en": "twirling umbrella, hopping over puddle, playful, dynamic pose"},
    {"w": 3, "zh": "迎着风张开双臂闭眼深呼吸",
     "en": "arms outstretched, closed eyes, wind, dynamic pose, hair flowing"},
    {"w": 3, "zh": "把书盖在脸上打盹",
     "en": "book covering face, sleeping, sitting"},
    {"w": 3, "zh": "蹲在草地上吹散手心的蒲公英",
     "en": "blowing dandelion seeds, kneeling on grass, seeds floating"},
    {"w": 3, "zh": "举着手机给自己拍立得式自拍",
     "en": "taking selfie, holding smartphone, self portrait"},
    {"w": 3, "zh": "低角度仰拍的腾空跳跃（裙摆与头发一齐飞扬）",
     "en": "jumping, mid-air, from below, dynamic pose, hair flowing, skirt flutter"},
    {"w": 3, "zh": "坐在地板上手撑着下巴出神",
     "en": "sitting on floor, chin rest, hand on own chin, spacing out"},
    {"w": 3, "zh": "伸手去够飘在半空的肥皂泡",
     "en": "reaching out, soap bubbles, chasing, dynamic pose, light particles"},
    # —— v0.37.0 生活瞬间第二批（继续对齐参考图的抓拍感与机位变化）——
    {"w": 4, "zh": "半躺在沙发上、双臂举过头顶舒展身体",
     "en": "lying on sofa, on back, arms above head, stretching, from side"},
    {"w": 3, "zh": "原地转圈、裙摆旋开飞扬",
     "en": "twirling, skirt flip, dynamic pose, motion"},
    {"w": 3, "zh": "坐在高脚凳上侧身回头",
     "en": "sitting on stool, turning around, looking back"},
    {"w": 3, "zh": "单膝跪在床上回眸",
     "en": "kneeling on bed, looking back, from behind"},
    {"w": 4, "zh": "抱着一束花低头轻嗅",
     "en": "holding bouquet, smelling flowers, closed eyes"},
    {"w": 3, "zh": "站在自动贩卖机前弯腰取饮料",
     "en": "vending machine, bending over, reaching out, night"},
    {"w": 4, "zh": "躺在草地上单手枕着头",
     "en": "lying on grass, on back, arm behind head"},
    {"w": 3, "zh": "逆光里抬手挡在眼前眺望远方",
     "en": "shielding eyes, hand raised, looking into distance, backlighting"},
    {"w": 3, "zh": "双手插兜倚着栏杆",
     "en": "hands in pockets, leaning on railing, relaxed"},
    {"w": 3, "zh": "拎着凉鞋赤脚走在沙滩边",
     "en": "carrying sandals, barefoot, walking on beach, water edge"},
    {"w": 3, "zh": "拉起连帽衫的兜帽低头快步走",
     "en": "hood up, walking, looking down, hands in pockets"},
    {"w": 3, "zh": "双手撑在栏杆上探头望向镜头",
     "en": "leaning forward on railing, looking at viewer, playful"},
    # 立绘模板专用（tpl_ 主题用 pose_kw「标准站姿」精确命中）：character sheet 惯例
    # 是双臂自然下垂、正面直视，不摆性格姿势
    {"w": 0, "zh": "双臂自然下垂的标准站姿", "en": "standing, arms at sides, straight-on, full body"},
]

# 构图/视图维度（v0.7.0）：默认不提及（普通单张立绘），低权重随机命中
# 全身站姿；「立绘模板」主题（THEMES 带 tpl_ 前缀）按 compose_kw 精确约束。
# v0.22.4：三/四视图项权重归零——它们**只能**由 tpl_* 模板主题的子集精确
# 命中，绝不参与随机。此前 3/30 的随机命中率让 base/lora 文生图链路（没套
# v0.13.3 的 force_single_view）把「character sheet, multiple views」写进
# 提示词，批量补图真的吐出三视图拼接设定稿。w=0 项仍在池里供模板命中与
# 面板显式锁定，_weighted_pick 对全 0 子集会返回该子集项。
COMPOSE = [
    {"w": 24, "zh": "", "en": ""},
    # v0.13.0：三/四视图模板文案按用户口径写死（正面 / 侧面 / 背面 + 灰色背景），
    # 单图项只描述构图，背景交给 background 维度（免得「纯色」与模板灰底打架）。
    {"w": 3, "zh": "正常站姿的单张全身立绘（正面朝向镜头）",
     "en": "simple background, standing, full body, front-facing"},
    {"w": 0, "zh": "三视图（正面、侧面、背面同框，灰色纯色背景）",
     "en": "character sheet, three views, front view, side view, back view, grey background"},
    {"w": 0, "zh": "四视图（正面、侧面、背面、四分之三侧面同框，灰色纯色背景）",
     "en": "character sheet, four views, front view, side view, back view, "
           "three-quarter view, grey background"},
]

# 取景维度（v0.25.7）：用户反馈「批量出的图基本都是全身照，立绘不一定非要全身，
# 半身特写也很好」。它和 COMPOSE 是两个正交维度——COMPOSE 管「有没有多视角拼接」，
# FRAMING 管「裁到身体哪个位置」。非全身项合计 17/24 ≈ 71%，默认明显偏半身与特写，
# 但保留 13% 不指定（交给底模与姿势自己决定）。
FRAMING = [
    {"w": 4, "zh": "", "en": ""},
    {"w": 7, "zh": "半身构图（裁到腰部以上）",
     "en": "cowboy shot, upper body visible"},
    {"w": 6, "zh": "胸像特写（裁到胸口以上，突出面部与表情细节）",
     "en": "close-up, portrait, upper body, face focus"},
    {"w": 4, "zh": "膝上构图（露出膝盖以上）",
     "en": "cowboy shot, knees visible"},
    {"w": 3, "zh": "全身构图（完整呈现站姿与鞋子）",
     "en": "full body, full length"},
]

# 表情维度（v0.26.3 重平衡）：用户实测 10 张里「惊讶地睁大眼睛」出现太频繁，
# 还常和泳装 / 婚纱这类主题完全不搭。改法两层：
#   ① 强情绪表情降权（惊讶瞪眼 5→2、捂嘴 3→1、吐舌 5→3、大笑 5→3），
#      补 3 款立绘常用的柔和表情（平静淡然 / 莞尔一笑 / 移开视线浅笑）；
#      柔和类合计约 47%，强情绪类压到个位数。
#   ② 每个内容主题配 expression_kw（见 THEMES），表情在主题子集内抽，
#      不再与服装 / 场景各自独立随机。
EXPRESSIONS = [
    {"w": 12, "zh": "温柔微笑", "en": "gentle smile, soft expression"},
    {"w": 8, "zh": "平静淡然的表情", "en": "calm, neutral expression, serene"},
    {"w": 7, "zh": "安静若有所思", "en": "pensive, thoughtful"},
    {"w": 7, "zh": "莞尔一笑（嘴角轻扬）", "en": "subtle smile, closed mouth"},
    {"w": 6, "zh": "害羞脸红", "en": "blush, shy"},
    {"w": 5, "zh": "微微移开视线、带着浅笑", "en": "looking away, soft smile"},
    {"w": 5, "zh": "无语地半眯着眼", "en": "expressionless, half-closed eyes"},
    {"w": 5, "zh": "闭眼享受阳光", "en": "closed eyes, peaceful expression"},
    {"w": 4, "zh": "得意的坏笑", "en": "smirk, smug"},
    {"w": 4, "zh": "困倦惺忪", "en": "sleepy"},
    {"w": 4, "zh": "星星眼兴奋表情", "en": "star-shaped eyes, excited"},
    {"w": 3, "zh": "俏皮地眨眼吐舌", "en": "wink, tongue out"},
    {"w": 3, "zh": "开怀大笑", "en": "laughing, open mouth"},
    {"w": 3, "zh": "傲娇地嘟起嘴", "en": "pout"},
    {"w": 2, "zh": "惊讶地睁大眼睛", "en": "surprised, wide-eyed"},
    {"w": 2, "zh": "微微歪头一脸疑惑", "en": "confused, tilting head"},
    {"w": 2, "zh": "得意地挺起胸膛", "en": "proud, confident smile"},
    {"w": 1, "zh": "惊讶地捂住嘴", "en": "surprised, covering mouth"},
]

# 姿势 → 表情锁定（v0.26.3）：睡着的姿势配「惊讶瞪眼」是硬伤，这类姿势直接
# 把表情收窄到困倦 / 闭眼 / 平静；其余姿势仍走主题子集或全池。
# v0.36.0：生活瞬间姿势扩后补两组——进食/饮品配开心系（吃西瓜配「平静淡然」
# 也是硬伤）、闭眼类动作配闭眼系（「闭眼听歌 + 星星眼」自相矛盾）。
_POSE_EXPR_LOCK = (
    (("打瞌睡", "蜷缩", "睡着", "侧躺", "仰面躺", "盖在脸上"), ["困倦", "闭眼", "平静"]),
    (("吃", "喝", "咬", "马克杯"), ["莞尔", "微笑", "星星眼", "眨眼"]),
    (("闭眼",), ["闭眼", "平静", "莞尔"]),
)

# 纯画风主题（art_*）通用的立绘表情池：排除需要特定情境的困倦 / 惊讶 / 疑惑 /
# 傲娇（用户实测「婚纱 + 困倦惺忪」违和）。只影响 art_* 主题，抽卡与内容主题
# 不受影响。
_GENERIC_EXPR_KWS = ("微笑", "平静", "若有所思", "莞尔", "浅笑",
                     "害羞", "半眯", "坏笑")

# body：v0.19.10 按用户口径重做——默认（90%）不提及体型 = 跟随参考图/角色原本
# 体型；变化合计压到 ~10% 且不再让「高挑成熟」一家独大（旧池 66/34 的变化概率、
# 高挑占变化里的 8/34，用户反馈「经常变成成熟高挑」）。补齐少女/萝莉两档。
# 手动选择：WebUI 生成立绘弹窗点「体型」chip 即可从本池（非空项按权重降序）选定。
BODY = [
    {"w": 90, "zh": "", "en": ""},
    {"w": 2, "zh": "身材高挑的成熟体型（成年女性比例）", "en": "tall female, mature female"},
    {"w": 2, "zh": "少女体型（年轻轻盈的女性比例）", "en": "young female, slim"},
    {"w": 2, "zh": "娇小体型（个子小、幼态比例）", "en": "petite"},
    {"w": 1, "zh": "萝莉体型（孩童般的比例）", "en": "child, petite"},
    {"w": 1, "zh": "胸部更丰满一些", "en": "large breasts"},
    {"w": 1, "zh": "胸部更纤细一些", "en": "small breasts"},
    {"w": 1, "zh": "健身后的紧致身材", "en": "toned, athletic"},
]

# 画风维度（v0.19.0）：用户反馈「主题里应该要加上画风，现在画风太单一」。
# 此前变化维度只有服装/背景/姿势/表情/体型，画风完全由底模决定——同一角色的
# 多张图看起来就像同一个画师同一套流程，换主题也只换了衣服和背景。
# 权重语义：空项 = 不写画风（交给底模自己的默认风格），保留一小部分避免
# 「每张都硬换风格」到失真；非空项按权重摇。
#
# 两批风格（v0.19.1 补第二批，用户又说前一批「太大众化」）：
#   ① 上色流派（前 16 款）：赛璐璐/厚涂/水彩/油画/水墨这类，换的是「上色方式」；
#   ② 介质与工艺（后 15 款）：印厂（丝网/Riso/复古广告印刷）、画材（蚀刻/淡彩/
#      岩彩/细密画/彩铅）、工艺（彩绘玻璃/剪纸/低多边形/黏土）、年代电子
#      （Y2K/故障 VHS）、镜头语言（电影感厚涂）、区域流派（美式漫画/韩漫）。
#      这一批换的是「这张图是怎么被做出来的」，构图层级/笔触/色域跟着一起变。
# 想固定成某一款：提示词面板的「画风」chip 里锁定，或选对应的「画风 · xxx」主题。
ARTSTYLES = [
    {"w": 8, "zh": "", "en": ""},
    # v0.25.7：用户反馈「画风大部分太复古，想要 2026 年的现代动漫画风」。
    # 现代系给最高权重，复古 / 工艺类整体降权（保留可手动选，但不再主导随机）。
    {"w": 12, "zh": "现代日式动画（干净线稿 + 柔和赛璐璐 + 通透高光）",
     "en": "modern anime illustration, clean lineart, soft cel shading, "
           "vivid colors, detailed eyes, crisp highlights, polished finish"},
    {"w": 11, "zh": "新海诚式电影动画（通透天空 + 体积光 + 高饱和渐变）",
     "en": "cinematic anime, luminous sky, volumetric light, god rays, "
           "vibrant gradient, hyper-detailed background, film-like composition"},
    {"w": 10, "zh": "清透厚涂插画（细腻笔触 + 柔和边缘光）",
     "en": "polished digital painting, soft brushwork, glossy highlights, "
           "subtle rim light, refined rendering, contemporary illustration"},
    {"w": 9, "zh": "轻小说插画（精致线稿 + 柔光 + 淡彩渐变）",
     "en": "light novel illustration, refined lineart, soft bloom, "
           "pastel gradient, delicate shading, modern anime art"},
    {"w": 8, "zh": "潮流插画（高级灰 + 大面积留白 + 图形化色块）",
     "en": "trendy illustration, muted premium palette, negative space, "
           "graphic shapes, editorial poster vibe, contemporary design"},
    {"w": 9, "zh": "日系动画赛璐璐上色",
     "en": "anime coloring, cel shading, clean lineart, crisp highlights"},
    {"w": 8, "zh": "厚涂半写实数字绘画",
     "en": "semi-realistic, thick painting, digital painting, detailed shading"},
    {"w": 7, "zh": "清透水彩绘本",
     "en": "watercolor, traditional media, soft colors, paper texture"},
    {"w": 3, "zh": "古典油画质感",
     "en": "oil painting, painterly, canvas texture, chiaroscuro"},
    {"w": 6, "zh": "赛博霓虹插画",
     "en": "cyberpunk, neon lights, high contrast, glowing, chromatic aberration"},
    {"w": 4, "zh": "暗黑哥特插画",
     "en": "gothic, dark fantasy, moody lighting, desaturated colors"},
    {"w": 2, "zh": "复古 90 年代动画",
     "en": "retro anime, 1990s, vintage, film grain, muted palette"},
    {"w": 3, "zh": "国风水墨淡彩",
     "en": "ink wash painting, chinese ink, sumi-e, muted colors"},
    {"w": 2, "zh": "浮世绘木刻版画",
     "en": "ukiyo-e, woodblock print, flat colors, japanese art"},
    {"w": 4, "zh": "黑白漫画网点",
     "en": "manga, monochrome, screentone, comic style, lineart"},
    {"w": 4, "zh": "粉彩少女漫画",
     "en": "shoujo manga, pastel colors, sparkles, delicate lineart"},
    {"w": 4, "zh": "3D 渲染动画风",
     "en": "3d render, cgi, smooth shading, subsurface scattering, ambient occlusion"},
    # v0.26.4：用户反馈「黏土手办风格有点丑」——权重归零，退出随机池。
    # w=0 的项 _weighted_pick 抽不到，但仍在池里：面板可以手动锁定它，
    # art_clay 主题（artstyle_kw 精确筛「黏土」）也仍然可用。
    {"w": 0, "zh": "黏土手办质感",
     "en": "clay render, figurine, plastic model, studio lighting"},
    {"w": 3, "zh": "像素艺术",
     "en": "pixel art, 16-bit, dithering"},
    {"w": 3, "zh": "彩色铅笔手绘",
     "en": "colored pencil, sketch, hand-drawn, hatching"},
    {"w": 4, "zh": "扁平矢量插画",
     "en": "flat color, vector art, thick outline, minimalist"},
    # —— v0.19.1：非主流 / 有介质感的画风。用户反馈「上面那些太大众化」——
    #    前 16 款本质都是「某种上色方式」，换个说法还是同一种插画流水线。这一批
    #    改从**创作介质与印刷工艺**切：印厂（丝网/孔版/复古广告印刷）、画材
    #    （蚀刻/淡彩/岩彩/细密画）、工艺（彩绘玻璃/剪纸/低多边形）、年代电子
    #    （Y2K/故障 VHS）、镜头语言（电影感厚涂）。介质一变，构图层级、笔触、
    #    色域都会跟着变，出来的图才是「另一路东西」而不是同一套 AI 味。
    {"w": 6, "zh": "钢笔淡彩速写",
     "en": "ink and watercolor, urban sketching, pen lineart, loose wash, "
           "white paper showing through"},
    {"w": 6, "zh": "电影感厚涂（逆光烟雾）",
     "en": "cinematic painting, volumetric light, rim light, atmospheric haze, "
           "shallow depth of field, film still composition"},
    {"w": 2, "zh": "复古印刷海报风",
     "en": "retro print, 1970s advertisement illustration, halftone print, "
           "limited palette, offset misregistration, paper grain"},
    {"w": 2, "zh": "岩彩重彩（矿物颜料）",
     "en": "mineral pigment, gold accent, mural painting, textured pigment, "
           "dunhuang mural colors"},
    {"w": 2, "zh": "铜版画蚀刻",
     "en": "etching, engraving, cross-hatching, fine lines, monochrome print, "
           "intaglio texture"},
    {"w": 2, "zh": "彩绘玻璃镶嵌",
     "en": "stained glass, leaded lines, luminous backlit colors, glass mosaic, "
           "gothic window panes"},
    {"w": 2, "zh": "Riso 孔版印刷",
     "en": "risograph, limited color palette, ink grain, misregistration, "
           "two-tone print"},
    {"w": 2, "zh": "故障艺术 VHS",
     "en": "glitch art, vhs, scanlines, chromatic aberration, datamosh, "
           "analog video noise"},
    {"w": 2, "zh": "Y2K 千禧金属",
     "en": "y2k aesthetic, chrome, liquid metal, holographic iridescence, "
           "metallic gradient, translucent plastic"},
    {"w": 3, "zh": "美式漫画",
     "en": "american comic, bold ink outlines, halftone shading, dynamic angle, "
           "dramatic contrast"},
    {"w": 5, "zh": "韩漫半写实",
     "en": "manhwa style, semi-realistic, clean lineart, soft cel shading, "
           "glossy highlights"},
    {"w": 4, "zh": "丝网印刷波普",
     "en": "pop art, screen print, bold flat colors, ben-day dots, "
           "high saturation"},
    {"w": 3, "zh": "细密画金箔",
     "en": "persian miniature, gold leaf, intricate ornament, "
           "decorative border, jewel tones"},
    {"w": 3, "zh": "剪纸纸雕",
     "en": "paper cutout, layered paper, craft texture, soft drop shadow, "
           "cut-paper silhouette"},
    {"w": 3, "zh": "低多边形体素",
     "en": "low poly, voxel art, isometric view, flat shading, geometric facets"},
]

# ---------- 鞋袜维度（v0.32.0）：跟随穿搭的 legwear + footwear ----------
# 此前只换服装、从不提鞋袜——全身照的鞋与裤袜永远跟参考图（泳装配乐福鞋、
# 婚纱配运动鞋就是这么来的）。danbooru 把这两族分得很细（kneehighs /
# thighhighs / pantyhose / bare legs / loafers / mary janes…），必须逐项点名。
# 机制：按「穿搭家族」给候选（_FEET_FAMILIES 关键词 → 候选列表），种子决定论
# 内摇（该光腿光腿、该丝袜丝袜）；穿搭为空项（跟随角色原本 / 模板底稿）时鞋袜
# 同样不提及；FOOTWEAR 池供面板 chip 锁定 / 重摇 / 启用（全池非空项）。
_F_JK = (
    {"w": 4, "zh": "黑色及膝袜与乐福鞋", "en": "black kneehighs, loafers"},
    {"w": 3, "zh": "白色短袜与乐福鞋", "en": "white socks, loafers"},
    {"w": 2, "zh": "黑色过膝袜与乐福鞋（绝对领域）",
     "en": "black thighhighs, loafers, zettai ryouiki"},
    {"w": 1, "zh": "光腿配乐福鞋", "en": "bare legs, loafers"},
)
_F_SPORT = (
    {"w": 4, "zh": "运动鞋与白色短袜", "en": "sneakers, white socks"},
    {"w": 2, "zh": "运动鞋（光腿）", "en": "sneakers, bare legs"},
)
_F_SWIM = (
    {"w": 5, "zh": "光脚", "en": "barefoot"},
    {"w": 2, "zh": "凉鞋", "en": "sandals"},
    {"w": 2, "zh": "光腿", "en": "bare legs"},
)
_F_KIMONO = (
    {"w": 4, "zh": "足袋与草履", "en": "tabi, zori"},
    {"w": 3, "zh": "光脚配木屐", "en": "geta, barefoot"},
)
_F_MAID = (
    {"w": 4, "zh": "黑色玛丽珍鞋与白色短袜", "en": "mary janes, white socks"},
    {"w": 3, "zh": "黑色玛丽珍鞋与黑色裤袜", "en": "mary janes, black pantyhose"},
)
_F_GOTHIC = (
    {"w": 3, "zh": "黑色过膝靴", "en": "black boots, thighhighs"},
    {"w": 2, "zh": "黑色裤袜配玛丽珍鞋", "en": "black pantyhose, mary janes"},
    {"w": 2, "zh": "黑色长靴", "en": "black boots"},
)
_F_HOME = (
    {"w": 6, "zh": "光脚", "en": "barefoot"},
    {"w": 2, "zh": "毛绒拖鞋", "en": "slippers"},
)
_F_IDOL = (
    {"w": 4, "zh": "黑色高筒靴", "en": "black knee boots"},
    {"w": 2, "zh": "短靴", "en": "ankle boots"},
    {"w": 2, "zh": "黑色高跟鞋", "en": "black high heels"},
)
_F_OFFICE = (
    {"w": 4, "zh": "黑色高跟鞋", "en": "black high heels"},
    {"w": 3, "zh": "黑色裤袜配高跟鞋", "en": "black pantyhose, high heels"},
    {"w": 2, "zh": "短靴", "en": "ankle boots"},
    {"w": 2, "zh": "光腿配高跟鞋", "en": "bare legs, high heels"},
)
_F_WINTER = (
    {"w": 4, "zh": "黑色长靴", "en": "black boots"},
    {"w": 2, "zh": "棕色长靴", "en": "brown boots"},
    {"w": 2, "zh": "褐色裤袜配短靴", "en": "brown pantyhose, ankle boots"},
)
_F_HEELS = (
    {"w": 3, "zh": "白色高跟鞋", "en": "white high heels"},
    {"w": 3, "zh": "黑色高跟鞋", "en": "black high heels"},
    {"w": 2, "zh": "凉鞋", "en": "sandals"},
    {"w": 2, "zh": "光腿配高跟鞋", "en": "bare legs, high heels"},
    {"w": 1, "zh": "白色裤袜", "en": "white pantyhose"},
)
_FEET_GENERIC = (
    {"w": 3, "zh": "黑色裤袜", "en": "black pantyhose"},
    {"w": 2, "zh": "运动鞋与白色短袜", "en": "sneakers, white socks"},
    {"w": 2, "zh": "凉鞋", "en": "sandals"},
    {"w": 2, "zh": "光腿", "en": "bare legs"},
    {"w": 2, "zh": "黑色长靴", "en": "black boots"},
)

# 街头混搭族（v0.37.0）：工装短裤 / Y2K / 皮夹克 / 挂脖露脐 / 层叠叠穿 /
# 异域华丽 / 滑肩开衫——鞋袜厚底鞋、帆布鞋、马丁靴、罗马凉鞋
_F_STREET = (
    {"w": 4, "zh": "厚底运动鞋", "en": "platform sneakers"},
    {"w": 3, "zh": "帆布鞋与白色短袜", "en": "sneakers, white socks"},
    {"w": 2, "zh": "马丁靴", "en": "combat boots"},
    {"w": 2, "zh": "罗马凉鞋", "en": "gladiator sandals"},
)
_F_STREET_KWS = ("工装", "Y2K", "牛仔", "皮夹克", "挂脖", "叠穿",
                 "异域", "部族", "开衫")

# 穿搭家族映射：按 outfit zh 子串命中（顺序即优先级——「运动系比基尼」必须先
# 命中泳装族再轮到运动族，故 SWIM 在 SPORT 之前；「花嫁和服」先命中和风族）。
_FEET_FAMILIES = (
    (("JK", "水手", "校服", "背带", "学院"), _F_JK),
    (("泳装", "泳衣", "比基尼", "纱笼", "防晒泳"), _F_SWIM),
    # v0.37.0：街头混搭族——「针织开衫JK制服」含开衫但 JK 行在前优先命中；
    # 「泳装外罩防晒开衫」泳装行在前；老条目均不含本行关键词，零影响
    (_F_STREET_KWS, _F_STREET),
    (("体操服", "运动", "拉拉队"), _F_SPORT),
    (("浴衣", "和服", "振袖", "访问着", "巫女", "袴"), _F_KIMONO),
    (("女仆", "围裙"), _F_MAID),
    (("哥特", "女巫", "吸血鬼", "魔法学院", "朋克"), _F_GOTHIC),
    (("睡衣", "睡袍", "睡裙", "居家", "家居服", "T恤", "卫衣"), _F_HOME),
    (("偶像", "打歌服", "舞台", "演出服"), _F_IDOL),
    (("大衣", "冬", "毛衣"), _F_WINTER),
    (("西装", "通勤", "铅笔裙", "航空", "职业", "衬衫"), _F_OFFICE),
    (("婚纱", "礼服", "秀禾", "白无垢", "旗袍", "连衣裙"), _F_HEELS),
)


def feet_candidates(outfit_zh: str):
    """穿搭 zh → 鞋袜候选列表（纯函数，可直测）。

    空穿搭返回空元组（鞋袜跟随角色原本，不提及）；无家族命中的穿搭回落
    泛用候选（_FEET_GENERIC）。
    """
    z = str(outfit_zh or "")
    if not z.strip():
        return ()
    for keys, cands in _FEET_FAMILIES:
        if any(k in z for k in keys):
            return cands
    return _FEET_GENERIC


def _dedupe_by_zh(items) -> tuple:
    seen = set()
    out = []
    for it in items:
        z = str(it.get("zh") or "")
        if z in seen:
            continue
        seen.add(z)
        out.append(it)
    return tuple(out)


FOOTWEAR = _dedupe_by_zh([
    *_F_JK, *_F_SPORT, *_F_SWIM, *_F_KIMONO, *_F_MAID, *_F_GOTHIC,
    *_F_HOME, *_F_IDOL, *_F_OFFICE, *_F_WINTER, *_F_HEELS, *_FEET_GENERIC,
])

# ---------- 头饰 / 外套维度（v0.34.1，取代 v0.33.0 的合并「帽子外套」） ----------
# 用户反馈合并处理太敷衍——实际情况是两件独立的事：
#   头饰 = 帽子/发带，看穿搭与场景合理戴、换或摘（JK 配贝雷帽/发带、夏日配
#          草帽/遮阳帽、冬装配毛线帽都很常见，不是一律摘掉）；
#   外套 = 穿在穿搭外面的那一层（开衫/牛仔外套/防晒衫/斗篷），看季节与穿搭
#          合理穿、换或脱（泳装配防晒开衫、连衣裙配开衫/牛仔外套）。
# 权重按真实搭配习惯调：头饰摘掉 ≈ 60%、外套脱掉 ≈ 73~80%，各家族不同——
# 「合理去掉或者更换，不要一成不变」。
# 自洽保护：穿搭条目本身就是帽子/外套的（女巫斗篷与尖顶帽、冬季呢子校服大衣、
# 运动外套与短裤），对应维度**沉默置空**——交给穿搭条目自己定义，避免
# 「大衣 + 脱掉外套」「尖顶帽 + 摘掉帽子」这种自相矛盾的指令。
# 文生图侧按维度分别剥离对应外观 tag（词边界匹配；不误伤 wardrobe/twintails，
# 不碰耳机/发饰/眼镜等角色固有物）；img2img 的 zh 条目是祈使短语直接作子句。
# 穿搭空项（跟随角色原本 / 模板底稿）时两维都不提及 = 帽子外套照旧保留。
_FHW_REMOVE = {"w": 8, "zh": "摘掉帽子", "en": "", "strip": 1}
_FHW_BERET = {"w": 3, "zh": "戴上贝雷帽", "en": "beret", "strip": 1}
_FHW_STRAW = {"w": 2, "zh": "戴上草帽", "en": "straw hat", "strip": 1}
_FHW_KNIT = {"w": 3, "zh": "戴上毛线帽", "en": "knit cap", "strip": 1}
_FHW_CAP = {"w": 2, "zh": "戴上棒球帽", "en": "baseball cap", "strip": 1}
_FHW_SUN = {"w": 2, "zh": "戴上宽檐遮阳帽", "en": "sun hat", "strip": 1}
_FHW_BAND = {"w": 1, "zh": "戴上发带", "en": "headband", "strip": 1}

_FHW_JK = (_FHW_REMOVE, _FHW_BERET, _FHW_BAND)        # 摘掉 ≈ 67%
_FHW_SUMMER = (_FHW_REMOVE, _FHW_STRAW, _FHW_SUN)     # 摘掉 ≈ 60%
_FHW_WINTER = (_FHW_REMOVE, _FHW_KNIT, _FHW_BERET)    # 摘掉 ≈ 62%
_FHW_SPORT = (_FHW_REMOVE, _FHW_CAP, _FHW_BAND)       # 摘掉 ≈ 73%
_FHW_IDOL = (_FHW_REMOVE, _FHW_BERET)
_FHW_PLAIN = (_FHW_REMOVE,)                            # 和风/女仆/哥特/婚纱等：一律摘掉
_FHW_GENERIC = (_FHW_REMOVE, _FHW_BERET, _FHW_STRAW, _FHW_KNIT)

# 头饰剥离词干：帽类外观（veil 头纱也是头饰）
_HEAD_STRIP_STEMS = ("hat", "cap", "beret", "hood", "helmet", "veil", "headband")

_HEADWEAR_FAMILIES = (
    (("JK", "水手", "校服", "背带", "学院"), _FHW_JK),
    (("泳装", "泳衣", "比基尼", "纱笼", "防晒泳"), _FHW_SUMMER),
    # v0.37.0：街头混搭族——棒球帽 / 发带 / 摘掉（放在连衣裙行前，
    # 「吊带连衣裙叠穿T恤」这类层叠街头款命中本行）
    (_F_STREET_KWS, (_FHW_REMOVE, _FHW_CAP, _FHW_BAND)),
    (("连衣裙", "碎花"), _FHW_SUMMER),
    (("体操服", "运动", "拉拉队"), _FHW_SPORT),
    (("浴衣", "和服", "振袖", "访问着", "巫女", "袴"), _FHW_PLAIN),
    (("女仆", "围裙"), _FHW_PLAIN),
    (("哥特", "女巫", "吸血鬼", "魔法学院", "朋克"), _FHW_PLAIN),
    (("睡衣", "睡袍", "睡裙", "居家", "家居服", "T恤", "卫衣"), _FHW_PLAIN),
    (("偶像", "打歌服", "舞台", "演出服"), _FHW_IDOL),
    (("大衣", "冬", "毛衣"), _FHW_WINTER),
    (("西装", "通勤", "铅笔裙", "航空", "职业", "衬衫"), _FHW_PLAIN),
    (("婚纱", "礼服", "秀禾", "白无垢", "旗袍"), _FHW_PLAIN),
)
# 自洽沉默：穿搭本身含帽（女巫斗篷与尖顶帽）→ 头饰沉默，交给穿搭条目定义
_HEAD_SILENT_KWS = ("帽",)


def headwear_candidates(outfit_zh: str):
    """穿搭 zh → 头饰候选（纯函数，可直测）。空穿搭返回空元组。"""
    z = str(outfit_zh or "")
    if not z.strip():
        return ()
    for keys, cands in _HEADWEAR_FAMILIES:
        if any(k in z for k in keys):
            return cands
    return _FHW_GENERIC


_FOW_REMOVE = {"w": 8, "zh": "脱掉外套", "en": "", "strip": 1}
_FOW_CARDIGAN = {"w": 2, "zh": "换上针织开衫", "en": "cardigan", "strip": 1}
_FOW_SUNCARD = {"w": 2, "zh": "换上防晒开衫", "en": "open cardigan", "strip": 1}
_FOW_DENIM = {"w": 1, "zh": "换上牛仔外套", "en": "denim jacket", "strip": 1}
_FOW_CAPE = {"w": 1, "zh": "披上斗篷", "en": "cape", "strip": 1}

_FOW_SWIM = (_FOW_REMOVE, _FOW_SUNCARD)               # 泳装 + 防晒开衫 ≈ 20%
_FOW_SUMMER = (_FOW_REMOVE, _FOW_CARDIGAN, _FOW_DENIM)  # 连衣裙 + 开衫/牛仔 ≈ 27%
_FOW_PLAIN = (_FOW_REMOVE,)

# 外套剥离词干：外套类外观（hoodie 是外套；hood 盖头归头饰）
_COAT_STRIP_STEMS = ("jacket", "coat", "cape", "cloak", "robe", "cardigan",
                     "poncho", "blazer", "hoodie")

_OUTERWEAR_FAMILIES = (
    (("泳装", "泳衣", "比基尼", "纱笼", "防晒泳"), _FOW_SWIM),
    # v0.37.0：街头混搭族——脱掉为主（层叠款不再叠一件外套）
    (_F_STREET_KWS, _FOW_PLAIN),
    (("连衣裙", "碎花"), _FOW_SUMMER),
    (("JK", "水手", "校服", "背带", "学院"), _FOW_PLAIN),
    (("体操服", "运动", "拉拉队"), _FOW_PLAIN),
    (("浴衣", "和服", "振袖", "访问着", "巫女", "袴"), _FOW_PLAIN),
    (("女仆", "围裙"), _FOW_PLAIN),
    (("哥特", "女巫", "吸血鬼", "魔法学院", "朋克"), _FOW_PLAIN),
    (("睡衣", "睡袍", "睡裙", "居家", "家居服", "T恤", "卫衣"), _FOW_PLAIN),
    (("偶像", "打歌服", "舞台", "演出服"), _FOW_PLAIN),
    (("大衣", "冬", "毛衣"), _FOW_PLAIN),
    (("西装", "通勤", "铅笔裙", "航空", "职业", "衬衫"), _FOW_PLAIN),
    (("婚纱", "礼服", "秀禾", "白无垢", "旗袍"), _FOW_PLAIN),
)
# 自洽沉默：穿搭本身就是外套（冬季呢子校服大衣、运动外套与短裤、v0.37.0 的
# 皮夹克/西装外套披肩）→ 外套沉默
_OUTER_SILENT_KWS = ("外套", "大衣", "斗篷", "披风", "开衫", "披肩", "夹克")


def outerwear_candidates(outfit_zh: str):
    """穿搭 zh → 外套候选（纯函数，可直测）。空穿搭返回空元组。"""
    z = str(outfit_zh or "")
    if not z.strip():
        return ()
    for keys, cands in _OUTERWEAR_FAMILIES:
        if any(k in z for k in keys):
            return cands
    return _FOW_PLAIN


def strip_tags_by_stems(tags_en: str, stems) -> str:
    """从 tags 串里剥掉命中词干的外观 tag（词边界匹配，保留其余与顺序）。"""
    out = []
    for seg in str(tags_en or "").split(","):
        s = seg.strip()
        if not s:
            continue
        low = s.lower()
        if any(re.search(r"\b" + re.escape(st) + r"\b", low) for st in stems):
            continue
        out.append(s)
    return ", ".join(out)


def _entry_tags(entry: dict, picks: dict | None = None) -> str:
    """角色外观 tags：头饰/外套维度为「摘掉/更换」时分别剥掉对应外观 tag。"""
    tags = str((entry or {}).get("tags_en") or "").strip().rstrip(",")
    hw = (picks or {}).get("headwear") or {}
    ow = (picks or {}).get("outerwear") or {}
    if hw.get("strip"):
        tags = strip_tags_by_stems(tags, _HEAD_STRIP_STEMS)
    if ow.get("strip"):
        tags = strip_tags_by_stems(tags, _COAT_STRIP_STEMS)
    return tags


# 穿搭派生维度（v0.32.0 鞋袜 / v0.34.1 头饰 / 外套）：
# (键, 候选函数, 自洽沉默关键词)——穿搭 zh 命中沉默关键词时该维度置空，
# 交给穿搭条目自己定义（女巫斗篷与尖顶帽 → 头饰与外套都沉默不提及）。
_FOLLOW_DIMS = (
    ("footwear", feet_candidates, ()),
    ("headwear", headwear_candidates, _HEAD_SILENT_KWS),
    ("outerwear", outerwear_candidates, _OUTER_SILENT_KWS),
)

# 人设固装（v0.35.0）：有的角色帽子/外套/鞋袜是角色特色（女巫的尖顶帽、
# 永远穿长靴的角色），摘掉或换掉就不符合人设。角色条目上的 keep_xxx 布尔
# 开关让对应派生维度**整维沉默**——种子随机永远不产出「摘掉帽子 / 脱掉外套」
# 这类指令，外观 tags 也不剥离（_entry_tags 只认 picks 的 strip 标记，维度
# 置空即不剥）。预览面板的显式操作（锁定 fixed / 重新启用 enabled）仍可覆盖：
# 那是管理员当面点的，属于用户意图优先——保护针对的是「随机抽中」。
_KEEP_ENTRY_FIELDS = (("footwear", "keep_footwear"),
                      ("headwear", "keep_headwear"),
                      ("outerwear", "keep_outerwear"))


def keep_dims_of(entry: dict) -> frozenset:
    """角色条目 → 人设固装维度集合（纯函数）。keep_xxx=True 的维度整维沉默。"""
    return frozenset(tag for tag, field in _KEEP_ENTRY_FIELDS
                     if (entry or {}).get(field))


def _follow_item(tag: str, out_zh: str, seed_int: int, keep=()) -> dict:
    """穿搭派生维度统一求值（纯函数）：自洽沉默、人设固装或空穿搭 → 置空不提及。"""
    z = str(out_zh or "").strip()
    fn, silent = next(((f, k) for t, f, k in _FOLLOW_DIMS if t == tag),
                      (None, ()))
    if fn is None or not z or tag in keep or any(k in z for k in silent):
        return {"w": 0, "zh": "", "en": ""}
    return _weighted_pick(fn(z), _sub(seed_int, f"{tag}|{z}", 2 ** 30))


# 维度池（面板 chip 锁定 / 重摇 / 启用用全池；种子初摇走家族派生不走全池）
HEADWEAR = _dedupe_by_zh([*_FHW_JK, *_FHW_SUMMER, *_FHW_WINTER, *_FHW_SPORT,
                          *_FHW_IDOL, *_FHW_GENERIC, *_FHW_PLAIN])
OUTERWEAR = _dedupe_by_zh([*_FOW_SWIM, *_FOW_SUMMER, *_FOW_PLAIN])


DIMS = {
    "compose": COMPOSE,
    "framing": FRAMING,
    "artstyle": ARTSTYLES,
    "outfit": OUTFITS,
    "footwear": FOOTWEAR,
    "headwear": HEADWEAR,
    "outerwear": OUTERWEAR,
    "background": BACKGROUNDS,
    "pose": POSES,
    "expression": EXPRESSIONS,
    "body": BODY,
}

# ---------- 角色固有特征（v0.25.7） ----------
# img2img 重绘最容易把「非人类部件」洗掉——用户实测「角色有尾巴，出来的图尾巴
# 没了」。只写一句笼统的「保持角色辨识度」不够，必须把尾巴/兽耳/角/翅膀这些
# 逐项点名。tags_en 是 danbooru 风格，直接子串匹配即可。
IDENTITY_FEATURES = (
    ("tail", "尾巴"), ("animal ears", "兽耳"), ("cat ears", "猫耳"),
    ("fox ears", "狐狸耳"), ("dog ears", "狗耳"), ("rabbit ears", "兔耳"),
    ("horns", "角"), ("dragon horns", "龙角"), ("single horn", "独角"),
    ("wings", "翅膀"), ("halo", "光环"), ("elf ears", "精灵耳"),
    ("pointy ears", "尖耳"), ("fangs", "虎牙"), ("ahoge", "呆毛"),
    ("antenna", "触角"), ("fin ears", "鱼鳍耳"),
    ("mechanical parts", "机械部件"), ("eyepatch", "眼罩"),
)


# 特征 tag 的词边界匹配（v0.29.5）：裸子串匹配会闹「twintails 包含 tail →
# 把双马尾当成尾巴、在每条编辑指令里点名『尾巴必须原样保留』」的笑话。
# 规则：整词命中，容忍词尾复数（tails / antennae / fang…都在 danbooru 常见
# 形态里）；复合词内部（twintails / ponytail / tailmouth）因无词边界不命中。
_FEATURE_RES = tuple(
    (en, zh, re.compile(r"\b" + re.escape(en) + r"(?:es|s|e)?\b"))
    for en, zh in IDENTITY_FEATURES
)


def identity_features(entry: dict) -> list:
    """从 tags_en 里挑出角色固有特征，返回 [(en, zh)]。纯函数。

    v0.29.5 起按词边界匹配（容忍复数），不再裸子串——`twintails` / `ponytail`
    不得再触发「尾巴」，`unicorn horn` 这类单数复合词也不该命中复数特征名。
    """
    tags = str((entry or {}).get("tags_en") or "").lower()
    if not tags:
        return []
    return [(en, zh) for en, zh, rx in _FEATURE_RES if rx.search(tags)]


def identity_feature_text(entry: dict, lang: str = "zh") -> str:
    """固有特征拼串（中文给编辑指令/中文描述，英文给 caption）。纯函数。"""
    feats = identity_features(entry)
    if lang == "en":
        return ", ".join(en for en, _zh in feats)
    return "、".join(zh for _en, zh in feats)


# ---------- 画风主题权重（v0.25.7） ----------
# `art_*` 纯画风主题在批量补图里等权洗牌时，复古 / 工艺类占了多数（用户反馈
# 「大部分太复古」）。这里给每个主题一个权重：现代系 3、中性 2、复古工艺 1。
# 只影响批量补图的主题轮换，不影响面板手动选择。
_ART_THEME_W = {
    "art_modern": 4, "art_shinkai": 4, "art_glossy": 3,
    "art_cinematic": 2, "art_manhwa": 2, "art_3d": 2, "art_flat": 2,
    "art_watercolor": 2, "art_penwash": 2, "art_cyber": 2,
    "art_retro": 1, "art_oil": 1, "art_ink": 1, "art_ukiyoe": 1,
    "art_gothic": 1, "art_retroprint": 1, "art_pigment": 1, "art_etching": 1,
    "art_stainglass": 1, "art_riso": 1, "art_glitch": 1, "art_y2k": 1,
    "art_manga": 1, "art_pixel": 1,
    # v0.26.4：黏土手办反馈丑 —— 批量轮换也不再选它（仍可在主题面板手动选）
    "art_clay": 0,
}


def art_theme_weight(theme_id: str) -> int:
    """`art_*` 主题在批量轮换里的相对权重（现代系更高）。纯函数。"""
    return int(_ART_THEME_W.get(str(theme_id or ""), 2))

# ---------- 主题预设（试画台「第 2 步：提示词」用） ----------
# {dim}_kw：从对应维度池里按中文子串筛子集，再在该子集内按种子加权
# （支持 outfit / background / pose / compose / expression / artstyle 六个维度，
# 缺省 = 不约束该维度）；筛不出子集（池里没这个词）就退回全池随机。custom 由用户
# 在预览面板自填。v0.19.0：每个内容主题都补了 artstyle_kw（画风收束到贴合该主题
# 的几种），另加 art_* 纯画风主题（整组图想统一成某种画风时直接选）。
# tpl_ 前缀 = 「立绘模板」：**固定底稿**，不带任何种子随机——构图 + 标准站姿 +
# 温柔微笑 + 纯色/灰底全部锁死；服装 / 画风 / 体型 / 取景四维 plain_dims 置空
# （提示词不提及 → 服装跟角色原始设定、画风交底模默认，character sheet 惯例）。
# 历史沿革：v0.7.2 曾置空变装，v0.7.4 按当时反馈改回随机穿搭；v0.29.3 用户明确
# 「模板是固定提示词，不能充满随机性」——随机感全部收回。想给模板换装 / 换画风，
# 用预览面板显式指定（outfit_override 与维度 chip / fixed 锁定不受 plain 影响）。
# v0.32.0：鞋袜一并置空（character sheet 惯例：鞋袜跟随角色原始设定）。
# v0.33.0：帽子外套一并置空（模板底稿保留角色原始的帽子与外套）。
_TPL_PLAIN = ["outfit", "footwear", "headwear", "outerwear",
              "artstyle", "body", "framing"]
THEMES = (
    {"id": "random", "label": "随机（按种子变化）", "outfit_kw": [], "bg_kw": []},
    {"id": "tpl_standing", "label": "【模板】纯色背景 · 正常站姿",
     "outfit_kw": [], "bg_kw": ["纯白色摄影棚"], "pose_kw": ["标准站姿"],
     "expression_kw": ["微笑"], "compose_kw": ["正常站姿"],
     "plain_dims": _TPL_PLAIN},
    # v0.13.0：单图 / 三视图 / 四视图三套底稿模板，统一灰底（character sheet 惯例），
    # 图生图配上「本次生成源」多选（正/侧/背分图）即可按参考图出底稿。
    {"id": "tpl_single", "label": "【模板】单图 · 正面全身（灰底）",
     "outfit_kw": [], "bg_kw": ["灰"], "pose_kw": ["标准站姿"],
     "expression_kw": ["微笑"], "compose_kw": ["单张全身立绘"],
     "plain_dims": _TPL_PLAIN},
    {"id": "tpl_3view", "label": "【模板】三视图（正/侧/背 · 灰底）",
     "outfit_kw": [], "bg_kw": ["灰"], "pose_kw": ["标准站姿"],
     "expression_kw": ["微笑"], "compose_kw": ["三视图"],
     "plain_dims": _TPL_PLAIN},
    {"id": "tpl_4view", "label": "【模板】四视图（正/侧/背/¾ · 灰底）",
     "outfit_kw": [], "bg_kw": ["灰"], "pose_kw": ["标准站姿"],
     "expression_kw": ["微笑"], "compose_kw": ["四视图"],
     "plain_dims": _TPL_PLAIN},
    # —— v0.14.0：主题子集全面扩充 + 关键词修正。教训：
    #    ① swimsuit 原只用「泳装」一词，漏掉池里叫「泳衣」的款式；
    #    ② office 原「西装」「马甲」两个宽泛词会把 JK 制服吸进职场子集；
    #    ③ idol 原 outfit 子集只有 1 项（永远打歌服）。
    #    关键词全部改成池条目命名自带的专属词（命中校验在 tests/test_prompt_gen.py）。
    # v0.26.3：内容主题全部补 expression_kw——表情跟着场景走，不再全池随机
    # （用户实测「泳装配惊讶瞪眼」这类违和组合，根因就是这十个主题一个都没配）。
    {"id": "swimsuit", "label": "泳装 · 夏日",
     "outfit_kw": ["泳装", "泳衣", "比基尼", "纱笼"], "bg_kw": ["海边", "泳池", "水上", "度假"],
     "expression_kw": ["微笑", "害羞", "闭眼享受", "开怀大笑"],
     "artstyle_kw": ["水彩", "赛璐璐", "扁平", "淡彩", "Riso"]},
    {"id": "campus", "label": "校园 · JK",
     "outfit_kw": ["水手服", "西装夹克", "针织开衫", "背带", "马甲衬衫JK", "百褶裙",
                   "运动外套", "校服", "体操服", "拉拉队"],
     "bg_kw": ["教室", "樱花", "天台", "走廊", "图书馆", "体育馆"],
     "expression_kw": ["微笑", "害羞", "俏皮", "若有所思"],
     "artstyle_kw": ["赛璐璐", "少女漫画", "水彩", "淡彩"]},
    {"id": "wedding", "label": "婚纱 · 典礼",
     "outfit_kw": ["婚纱", "礼服", "秀禾", "白无垢"], "bg_kw": ["纯白", "浅粉", "教堂", "拱门", "宴会"],
     "expression_kw": ["温柔微笑", "害羞", "闭眼享受", "若有所思"],
     "artstyle_kw": ["油画", "水彩", "赛璐璐", "彩绘玻璃", "细密画", "岩彩"]},
    {"id": "office", "label": "职场 · 通勤",
     "outfit_kw": ["职业西装", "西装裤装", "通勤", "铅笔裙", "西裤", "毛衣", "航空制服"],
     "bg_kw": ["咖啡馆", "霓虹", "办公室", "写字楼", "天桥"],
     "expression_kw": ["微笑", "坏笑", "半眯", "若有所思"],
     "artstyle_kw": ["厚涂", "扁平", "赛璐璐", "复古印刷", "韩漫"]},
    {"id": "kimono", "label": "和风 · 浴衣",
     "outfit_kw": ["浴衣", "和服", "振袖", "访问着", "巫女"], "bg_kw": ["和室", "樱花", "祭典", "温泉", "枯山水"],
     "expression_kw": ["温柔微笑", "若有所思", "害羞", "平静"],
     "artstyle_kw": ["浮世绘", "水墨", "水彩", "岩彩", "淡彩"]},
    {"id": "maid", "label": "女仆 · 咖啡厅",
     "outfit_kw": ["女仆"], "bg_kw": ["咖啡馆", "咖啡店", "客厅", "沙发"],
     "expression_kw": ["微笑", "害羞", "俏皮"],
     "artstyle_kw": ["赛璐璐", "水彩", "扁平", "Riso"]},
    {"id": "gothic", "label": "暗黑 · 哥特",
     "outfit_kw": ["哥特", "女巫", "吸血鬼", "魔法学院"], "bg_kw": ["霓虹", "雨", "古堡", "烛光", "墓园"],
     "expression_kw": ["半眯", "若有所思", "坏笑", "平静"],
     "artstyle_kw": ["哥特", "油画", "铜版画", "彩绘玻璃", "故障"]},
    {"id": "home", "label": "居家 · 慵懒",
     "outfit_kw": ["睡衣", "睡袍", "睡裙", "T恤", "卫衣", "家居服", "毛衣"],
     "bg_kw": ["雨", "和室", "咖啡馆", "客厅", "卧室"],
     "expression_kw": ["困倦", "温柔微笑", "闭眼享受", "若有所思"],
     "artstyle_kw": ["水彩", "少女漫画", "赛璐璐", "淡彩", "剪纸"]},
    {"id": "idol", "label": "偶像 · 舞台",
     "outfit_kw": ["打歌服", "舞台", "演出服"], "bg_kw": ["圣诞", "霓虹", "星空", "演唱会", "直播间"],
     "expression_kw": ["星星眼", "开怀大笑", "俏皮", "微笑"],
     "artstyle_kw": ["赛博", "3D 渲染", "赛璐璐", "Y2K", "故障"]},
    {"id": "party", "label": "旗袍 · 东方",
     "outfit_kw": ["旗袍"], "bg_kw": ["和室", "樱花", "绿意花园", "中式庭院", "红灯笼"],
     "expression_kw": ["温柔微笑", "害羞", "若有所思", "平静"],
     "artstyle_kw": ["水墨", "油画", "浮世绘", "岩彩", "细密画"]},
    # —— v0.19.0：纯画风主题。只约束 artstyle 维度（服装/背景/姿势仍按种子随机），
    #    适合「让这角色的这一批图统一成某种画风」；批量补图的内容主题轮换会自动
    #    带上它们（batch_fill.content_theme_ids 排除的只有 random 与 tpl_）。
    # v0.25.7：现代动漫画风主题（批量轮换里权重最高，见 art_theme_weight）
    {"id": "art_modern", "label": "画风 · 现代日式动画",
     "artstyle_kw": ["现代日式动画"]},
    {"id": "art_shinkai", "label": "画风 · 电影感动画（通透天空）",
     "artstyle_kw": ["新海诚式"]},
    {"id": "art_glossy", "label": "画风 · 清透厚涂",
     "artstyle_kw": ["清透厚涂"]},
    {"id": "art_watercolor", "label": "画风 · 清透水彩", "artstyle_kw": ["水彩"]},
    {"id": "art_oil", "label": "画风 · 古典油画", "artstyle_kw": ["油画"]},
    {"id": "art_ink", "label": "画风 · 国风水墨", "artstyle_kw": ["水墨"]},
    {"id": "art_ukiyoe", "label": "画风 · 浮世绘", "artstyle_kw": ["浮世绘"]},
    {"id": "art_cyber", "label": "画风 · 赛博霓虹", "artstyle_kw": ["赛博"]},
    {"id": "art_gothic", "label": "画风 · 暗黑哥特", "artstyle_kw": ["哥特"]},
    {"id": "art_retro", "label": "画风 · 复古 90s", "artstyle_kw": ["复古"]},
    {"id": "art_manga", "label": "画风 · 黑白漫画", "artstyle_kw": ["漫画"]},
    {"id": "art_3d", "label": "画风 · 3D 渲染", "artstyle_kw": ["3D 渲染"]},
    {"id": "art_clay", "label": "画风 · 黏土手办", "artstyle_kw": ["黏土"]},
    {"id": "art_pixel", "label": "画风 · 像素艺术", "artstyle_kw": ["像素"]},
    {"id": "art_flat", "label": "画风 · 扁平插画", "artstyle_kw": ["扁平"]},
    # —— v0.19.1：介质系纯画风主题（对应上面新加的 15 款非主流画风，只挑最有
    #    辨识度的 10 个单独开主题；其余仍可在提示词面板的「画风」chip 里锁定）
    {"id": "art_penwash", "label": "画风 · 钢笔淡彩", "artstyle_kw": ["淡彩"]},
    {"id": "art_cinematic", "label": "画风 · 电影感厚涂", "artstyle_kw": ["电影感"]},
    {"id": "art_retroprint", "label": "画风 · 复古印刷海报", "artstyle_kw": ["复古印刷"]},
    {"id": "art_pigment", "label": "画风 · 岩彩重彩", "artstyle_kw": ["岩彩"]},
    {"id": "art_etching", "label": "画风 · 铜版画蚀刻", "artstyle_kw": ["蚀刻"]},
    {"id": "art_stainglass", "label": "画风 · 彩绘玻璃", "artstyle_kw": ["彩绘玻璃"]},
    {"id": "art_riso", "label": "画风 · Riso 孔版", "artstyle_kw": ["Riso"]},
    {"id": "art_glitch", "label": "画风 · 故障 VHS", "artstyle_kw": ["故障"]},
    {"id": "art_y2k", "label": "画风 · Y2K 千禧", "artstyle_kw": ["Y2K"]},
    {"id": "art_manhwa", "label": "画风 · 韩漫半写实", "artstyle_kw": ["韩漫"]},
)


def get_theme(theme_id: str) -> dict | None:
    tid = str(theme_id or "").strip()
    if not tid or tid == "random":
        return None
    return next((t for t in THEMES if t["id"] == tid), None)


def content_theme_ids() -> list:
    """内容主题 id（排除 random 与 tpl_ 模板；batch_fill 轮换池同口径）。"""
    return [str(t["id"]) for t in THEMES
            if str(t.get("id") or "") != "random"
            and not str(t.get("id") or "").startswith("tpl_")]


def resolve_theme(theme_id: str, seed_int: int) -> tuple[dict | None, str]:
    """解析主题 id → (theme, 实际生效 id)。

    v0.20.0 起「random」不再是「不套主题」：按种子从内容主题池等权摇一个
    （决定论，同种子恒定；试画台预览与实际生成各自调用本函数，结果一致，
    摇中的主题 id 可随档记录、详情弹窗能看到「随机到底摇出了什么主题」）。
    v0.29.10：摇池排除 art_theme_weight == 0 的退池画风（art_clay）——v0.26.4
    只把权重清在批量轮换（theme_plan）里，面板「随机」仍能摇中黏土手办主题，
    等于把用户明确嫌丑的风格从主题后门塞回来（种子按 角色+日期+seq 恒定，
    同角色当天次次都是黏土）。要黏土请在主题下拉显式选「画风 · 黏土手办」。
    空串/未知 id 维持不套主题（抽卡主链路现状：维度纯随机）。
    """
    tid = str(theme_id or "").strip()
    if tid != "random":
        return get_theme(tid), tid
    ids = content_theme_ids()
    ids = [i for i in ids if art_theme_weight(i) > 0]
    if not ids:
        return None, tid
    rid = ids[_sub(seed_int, "theme_roll", 2 ** 30) % len(ids)]
    return get_theme(rid), rid


def apply_theme(picks: dict, seed_int: int, theme: dict | None) -> dict:
    """主题约束：带维度关键词的主题在对应维度池子集内重摇（其余维度保持种子随机）。

    支持 artstyle / outfit / background / pose / compose / expression 六维
    （v0.7.0 起 pose/compose，v0.7.2 起 expression，v0.19.0 起 artstyle）；
    ⚠️ background 的主题键是历史沿革的 bg_kw（不是 background_kw），别写错。
    plain_dims：列出的维度直接置空（zh/en 均空 = 提示词不提及）——机制保留
    （v0.7.2 曾用它关掉模板变装，v0.7.4 起模板恢复随机穿搭，当前无主题使用）。
    纯函数：同 (seed, theme) 恒定；子集为空时不动（回全池结果）。
    """
    if not theme:
        return picks
    for dim in _THEME_KW_KEYS:
        kws = _theme_kws(dim, theme)
        if not kws:
            continue
        subset = [it for it in DIMS[dim]
                  if any(kw in str(it.get("zh") or "") for kw in kws)]
        if subset:
            picks[dim] = _weighted_pick(
                subset, _sub(seed_int, f"{dim}@{theme.get('id')}", 2 ** 30))
    for dim in (theme.get("plain_dims") or []):
        if str(dim) in DIMS:
            picks[str(dim)] = {"w": 0, "zh": "", "en": ""}
    return picks


# ---------- 种子派生（决定论：同种子同维度同选择） ----------

def _sub(seed_int: int, tag: str, modulus: int) -> int:
    if modulus <= 0:
        return 0
    raw = f"{int(seed_int)}|{tag}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return int.from_bytes(digest[:8], "big") % modulus


# ---------- 维度抽签（v0.14.0 提取：全池 / 主题子集两条路径共用） ----------

# 主题的维度关键词键名。⚠️ background 是历史沿革的 bg_kw，别写错。
_THEME_KW_KEYS = {"artstyle": "artstyle_kw", "outfit": "outfit_kw",
                  "background": "bg_kw", "pose": "pose_kw",
                  "compose": "compose_kw", "expression": "expression_kw"}


def _theme_kws(dim: str, theme: dict | None) -> list:
    """该主题对该维度配置的关键词列表（无约束主题/维度 → 空表）。"""
    key = _THEME_KW_KEYS.get(dim)
    if not key or not theme:
        return []
    return [str(k).strip() for k in (theme.get(key) or []) if str(k).strip()]


def _pick_dim(dim: str, seed_int: int, theme: dict | None = None,
              non_empty_only: bool = False) -> dict:
    """单维度抽签：主题带该维度关键词 → 子集内摇（tag 混入主题 id，与
    apply_theme 历史行为逐位一致）；否则全池摇。non_empty_only 用于「显式启用」
    默认沉默的维度（compose/body）：子集内没有非空项时才回落全池。"""
    items = DIMS[dim]
    kws = _theme_kws(dim, theme)
    if kws:
        subset = [it for it in items
                  if any(kw in str(it.get("zh") or "") for kw in kws)]
        if subset:
            return _weighted_pick(
                subset, _sub(seed_int, f"{dim}@{theme.get('id')}", 2 ** 30))
    if non_empty_only:
        # w=0 是模板专用项（三/四视图、标准站姿）：面板「启用该维度」也不该
        # 随机抽中；模板主题走上面的 kws 子集分支，不受这层过滤影响。
        ne = [it for it in items
              if str(it.get("zh") or "").strip() and int(it.get("w", 1)) > 0]
        if ne:
            items = ne
    return _weighted_pick(items, _sub(seed_int, dim, 2 ** 30))


def _fixed_item(dim: str, zh: str) -> dict:
    """锁定项：zh 精确匹配池内条目拿英文 tag；池外自定义短语用 zh 兜底 en。"""
    zh = str(zh or "").strip()[:80]
    if not zh:
        return {"w": 0, "zh": "", "en": ""}
    en = ""
    for it in DIMS[dim]:
        if str(it.get("zh") or "") == zh:
            en = str(it.get("en") or "")
            break
    return {"w": 0, "zh": zh, "en": en or zh}


def dim_pool_options() -> dict:
    """预览面板维度下拉用：每个维度池的非空中文短语（按权重降序）。"""
    out = {}
    for dim, items in DIMS.items():
        ne = [it for it in items if str(it.get("zh") or "").strip()]
        ne.sort(key=lambda it: -int(it.get("w", 1)))
        out[dim] = [str(it.get("zh") or "") for it in ne]
    return out


# 服装配色（v0.13.1）：款式池里不再写死颜色，颜色按种子单独抽，再拼到款名前。
# 大权重空项 = 保留款式本来配色；否则旗袍/婚纱这类经典色会永远压在同一色上
# （用户反馈「旗袍每次都是黑色」）。
OUTFIT_COLORS = [
    {"w": 46, "zh": "", "en": ""},
    {"w": 7, "zh": "白色", "en": "white"},
    {"w": 6, "zh": "黑色", "en": "black"},
    {"w": 5, "zh": "藏青色", "en": "navy blue"},
    {"w": 5, "zh": "酒红色", "en": "wine red"},
    {"w": 5, "zh": "墨绿色", "en": "dark green"},
    {"w": 4, "zh": "浅蓝色", "en": "light blue"},
    {"w": 4, "zh": "香槟金色", "en": "champagne gold"},
    {"w": 4, "zh": "浅粉色", "en": "light pink"},
    {"w": 4, "zh": "米白色", "en": "off-white"},
    {"w": 3, "zh": "浅紫色", "en": "light purple"},
    {"w": 3, "zh": "灰色", "en": "grey"},
    {"w": 3, "zh": "棕色", "en": "brown"},
    {"w": 2, "zh": "正红色", "en": "red"},
]


def _weighted_pick(items: list, r: int) -> dict:
    total = sum(int(it.get("w", 1)) for it in items) or 1
    x = int(r) % total
    for it in items:
        x -= int(it.get("w", 1))
        if x < 0:
            return it
    return items[-1]


def _apply_outfit_color(picks: dict, color: dict) -> None:
    """把配色条目拼到款式前（zh 前缀 / en 前置 tag）；空配色或空款式不拼。"""
    base = picks.get("outfit") or {}
    if str(color.get("zh") or "") and str(base.get("zh") or ""):
        picks["outfit"] = {
            "w": base.get("w", 1),
            "zh": f"{color['zh']}{base['zh']}",
            "en": f"{color['en']}, {base['en']}",
        }


def pick_variations(seed_int: int, theme: dict | None = None,
                    outfit_override: str = "", keep=()) -> dict:
    """按种子从各维度池确定性挑选（纯函数，可测）。返回 {维度: 条目}。

    theme 传入主题预设时，约束维度在主题子集内重摇（_pick_dim 与历史
    「先全池再 apply_theme 重摇」逐位等价）。
    outfit_override 非空 = 用户指定穿搭（预览面板「穿搭」输入框）：
    整体覆盖 outfit 维度（zh/en 同文——任意自定义服装没有池内英文对照，
    LLM 通道与文生图 caption 都能吃中文，danbooru tag 串为次优兜底）。
    空串 = 不干预（主题子集重摇或全池随机）。
    v0.32.0：鞋袜维度不参与独立随机——先定穿搭，再按「穿搭家族」派生鞋袜
    （见 feet_candidates）；穿搭空项（跟随角色原本 / 模板底稿）时鞋袜同样
    不提及。
    v0.34.1：头饰 / 外套两维同构派生（见 headwear_candidates /
    outerwear_candidates，自洽沉默规则见 _FOLLOW_DIMS）。
    v0.35.0：keep = 人设固装维度集合（keep_dims_of(entry)）——集合内的
    派生维度整维沉默，随机不产出「摘掉帽子」类违背人设的指令。
    """
    picks = {tag: _pick_dim(tag, seed_int, theme) for tag in DIMS
             if tag not in ("footwear", "headwear", "outerwear")}
    # plain_dims：列出的维度直接置空（机制保留，当前无主题使用）
    for dim in ((theme or {}).get("plain_dims") or []):
        if str(dim) in DIMS:
            picks[str(dim)] = {"w": 0, "zh": "", "en": ""}
    # v0.13.1：服装配色按种子独立抽（款式池已不含颜色），拼到款名前；
    # 空项 = 保留款式本来配色。用户显式指定穿搭时不再叠颜色前缀。
    color = _weighted_pick(OUTFIT_COLORS, _sub(seed_int, "outfit_color", 2 ** 30))
    _apply_outfit_color(picks, color)
    text = str(outfit_override or "").strip().strip("。，,")[:80]
    if text:
        picks["outfit"] = {"w": 0, "zh": text, "en": text}
    # v0.32.0：鞋袜跟随穿搭——穿搭 zh 非空才派生（家族候选内按种子摇）；
    # 空穿搭（含模板 plain_dims）置空 = 鞋袜跟随角色原本设定。
    # v0.34.1：头饰 / 外套同构派生（自洽沉默规则在 _follow_item 内）。
    _plain = {str(d) for d in ((theme or {}).get("plain_dims") or [])}
    out_zh = str((picks.get("outfit") or {}).get("zh") or "").strip()
    for tag, _fn, _silent in _FOLLOW_DIMS:
        if tag in _plain:
            picks[tag] = {"w": 0, "zh": "", "en": ""}
        else:
            picks[tag] = _follow_item(tag, out_zh, seed_int, keep)
    # v0.26.3：纯画风主题（art_*）只锁画风、不约束表情，实测会出现「婚纱 +
    # 困倦惺忪」这类违和。给一个通用立绘表情池（排除需要特定情境的困倦 /
    # 惊讶 / 疑惑），让纯画风主题下的表情也落在自然范围。
    if theme and str(theme.get("id") or "").startswith("art_") \
            and not _theme_kws("expression", theme):
        generic = [it for it in EXPRESSIONS
                   if any(kw in str(it.get("zh") or "")
                          for kw in _GENERIC_EXPR_KWS)]
        if generic:
            picks["expression"] = _weighted_pick(
                generic, _sub(seed_int, "expression@art_theme", 2 ** 30))
    # v0.26.3：姿势与表情强相关——睡着 / 躺着的姿势配「惊讶瞪眼」是硬伤。
    # 与主题子集取交集（睡姿优先）；交集为空才退到睡姿子集，避免覆盖掉主题
    # 已收窄的表情范围（泳装主题的「闭眼享受阳光」正好两边都合适）。
    pose_zh = str((picks.get("pose") or {}).get("zh") or "")
    for hints, expr_kws in _POSE_EXPR_LOCK:
        if not any(h in pose_zh for h in hints):
            continue
        lock_pool = [it for it in EXPRESSIONS
                     if any(kw in str(it.get("zh") or "") for kw in expr_kws)]
        theme_kws = _theme_kws("expression", theme)
        if theme_kws and lock_pool:
            themed = [it for it in EXPRESSIONS
                      if any(kw in str(it.get("zh") or "") for kw in theme_kws)]
            both = [it for it in lock_pool if it in themed]
            if both:
                lock_pool = both
        if lock_pool:
            picks["expression"] = _weighted_pick(
                lock_pool, _sub(seed_int, "expression@pose_lock", 2 ** 30))
        break
    return picks


def apply_dim_state(picks: dict, seed_int: int, dim_state: dict | None,
                    theme: dict | None = None,
                    keep=()) -> dict:
    """预览面板的维度级控制（v0.14.0）：单独重摇 / 锁定 / 删除 / 启用，纯函数。

    dim_state 字段（全部可选，未知维度与字段忽略）：
    - attempts: {dim: n}  n>0 的维度用 seed_int + n*7919 重摇（v0.29.10 起从
                          **全池**非空项摇、不再困在主题子集——art_* 主题的
                          artstyle 子集普遍只有 1~2 项，子集内重摇等于没摇
                          （实测画风永远黏土手办）；outfit 重摇时配色同种子
                          重抽，避免「换款式不换色」）
    - enabled: [dim]      显式启用默认沉默的维度（compose/body）：若当前为空项，
                          从该维度非空项里重挑（主题子集内）
    - fixed: {dim: zh}    锁定为指定中文短语（en 从池内按 zh 反查，查不到用 zh
                          兜底；不叠配色前缀——显式选定值所见即所得）
    - disabled: [dim]     本轮不提及该维度（置空，提示词链路自动跳过）
    优先级（后覆盖前）：attempts < enabled < fixed < disabled——用户显式操作
    都优先于种子随机。调用方须在 force_single_view 之后调用：面板显式启用的
    compose 应能覆盖图生图的强制单图（用户意图优先，v0.13.3 只拦「随机命中」）。
    v0.35.0：keep = 人设固装维度集合——穿搭重摇/锁定后的派生维度重摇尊重
    keep（随机不许碰人设固装）；enabled / fixed 是面板上的显式操作，不拦
    （用户意图优先，见 _KEEP_ENTRY_FIELDS 注释）。
    """
    st = dim_state if isinstance(dim_state, dict) else {}
    out = dict(picks)
    fixed = st.get("fixed") if isinstance(st.get("fixed"), dict) else {}
    attempts = st.get("attempts") if isinstance(st.get("attempts"), dict) else {}
    enabled = st.get("enabled") if isinstance(st.get("enabled"), list) else []
    disabled = st.get("disabled") if isinstance(st.get("disabled"), list) else []
    for dim, n in attempts.items():
        dim = str(dim)
        if dim not in DIMS or dim in fixed:
            continue
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        sub_seed = (seed_int + n * 7919) % (2 ** 31)
        # v0.29.10：🎲 单独随机 = 从该维度**全池**重摇（非空项；w=0 项加权抽
        # 天然抽不中），不再困在主题子集里——art_* 主题（占内容主题约七成）的
        # artstyle 子集普遍只有 1~2 项，art_clay 更是单元素子集（只含 v0.26.4
        # 起权重归零的「黏土手办质感」），子集内重摇点了等于没点（用户实测
        # 「画风永远黏土手办」）。主题子集只管种子初摇；显式点骰子 = 用户明确
        # 要换一个，与下拉选项（dim_pool_options 全池）同一口径。non_empty_only
        # 让已展示的维度重摇后仍非空（compose/body/framing 不会被摇成空项消失）。
        out[dim] = _pick_dim(dim, sub_seed, None, non_empty_only=True)
        if dim == "outfit":
            color = _weighted_pick(
                OUTFIT_COLORS, _sub(sub_seed, "outfit_color", 2 ** 30))
            _apply_outfit_color(out, color)
            # v0.32.0/v0.34.1：重摇穿搭后鞋袜、头饰、外套跟着新穿搭的家族候选
            # 重摇（该维度被锁定/禁用时不动——用户显式操作优先）
            new_zh = str((out.get("outfit") or {}).get("zh") or "").strip()
            for tag, _fn, _silent in _FOLLOW_DIMS:
                if tag in fixed or tag in disabled:
                    continue
                out[tag] = _follow_item(tag, new_zh, sub_seed, keep)
    for dim in enabled:
        dim = str(dim)
        if dim not in DIMS or dim in fixed:
            continue
        if not str((out.get(dim) or {}).get("zh") or "").strip():
            if dim in ("footwear", "headwear", "outerwear"):
                # v0.32.0/v0.34.1：重新启用 = 跟随当前穿搭的家族候选（不是全池
                # 乱摇；自洽沉默规则照常生效）。v0.35.0：不传 keep——面板显式
                # 启用是用户当面操作，人设固装拦的是随机命中，不拦显式操作
                z = str((out.get("outfit") or {}).get("zh") or "").strip()
                out[dim] = _follow_item(dim, z, seed_int)
            else:
                out[dim] = _pick_dim(dim, seed_int, theme, non_empty_only=True)
    for dim, zh in fixed.items():
        dim = str(dim)
        if dim in DIMS:
            out[dim] = _fixed_item(dim, zh)
            if dim == "outfit":
                # v0.32.0/v0.34.1：锁定穿搭后鞋袜、头饰、外套跟随锁定穿搭重派生
                # （v0.35.0：重派生尊重 keep——锁穿搭不是解锁人设固装）
                z = str((out.get("outfit") or {}).get("zh") or "").strip()
                for tag, _fn, _silent in _FOLLOW_DIMS:
                    if tag in fixed:
                        continue
                    out[tag] = _follow_item(tag, z, seed_int, keep)
    for dim in disabled:
        dim = str(dim)
        if dim in DIMS:
            out[dim] = {"w": 0, "zh": "", "en": ""}
        if dim == "outfit":
            # v0.32.0/v0.34.1：本轮不提穿搭 → 鞋袜、头饰、外套也不提（否则与
            # 未提及的穿搭脱节；该维度自身被锁定/禁用时不动）
            for tag, _fn, _silent in _FOLLOW_DIMS:
                if tag not in fixed and tag not in disabled:
                    out[tag] = {"w": 0, "zh": "", "en": ""}
    return out


def _compose_is_multi(picks: dict) -> bool:
    """本轮是否要求多视图底稿（三/四视图模板）。LLM 提示与兜底文案都要跟着改。"""
    zh = str((picks.get("compose") or {}).get("zh") or "")
    return ("三视图" in zh) or ("四视图" in zh)


def force_single_view(picks: dict) -> dict:
    """图生图编辑模式（非模板主题）把 compose 维度置空（v0.13.3）。

    COMPOSE 池有约 10% 权重随机命中三/四视图——落到编辑指令就是「把构图改为
    三视图」，以多视图设定稿为源重绘立绘时输出仍是拼接设定稿（用户实测：
    「根据多视图生成的立绘也是多视图」）；且种子决定论让重试也复现。
    图生图的默认预期是单张立绘：想出设定稿请显式选 tpl_ 主题（其 compose_kw
    会重新约束本维度）。纯函数，返回新 dict。
    """
    out = dict(picks)
    out["compose"] = {"w": 0, "zh": "", "en": ""}
    return out


# ---------- 参考图形态（v0.29.4）：指令按「无图 / 单图 / 多图」分流 ----------

def template_ref_mode(entry: dict) -> str:
    """本次图生图参考图形态：none（无参考图）/ single（1 张）/ multi（多张）。

    口径与 draw.generate_portrait 完全一致：只认 strategy 里**显式**的图生图源
    （gen_refs 多图 + 主参考 ref，去重、ref 在前），不碰资料库 refs（参考图墙
    只是资料库，不是生成源）。img2img 缺源时 draw 侧会降级文生图，提示词这边
    必须同步按 none 走——否则「指令说改参考图、实际按 tags 文生图」两边打架。
    纯函数。
    """
    if str((entry or {}).get("strategy_type") or "base") != "img2img":
        return "none"
    st = entry.get("strategy") or {}
    if not isinstance(st, dict):
        return "none"
    raw = st.get("gen_refs")
    refs = ([str(x).strip() for x in raw if str(x or "").strip()]
            if isinstance(raw, list) else [])
    ref = str(st.get("ref") or "").strip()
    if ref and ref not in refs:
        refs.insert(0, ref)
    if not refs:
        return "none"
    return "multi" if len(refs) > 1 else "single"


def _ref_clause_multi_view(mode: str) -> str:
    """多视图模板（三/四视图）按参考图形态给的分句（含句首分号）。"""
    if mode == "single":
        return ("；参考图只有一张（通常是正面）：以参考图中的角色形象与服装为"
                "基准补全其余视角，参考图未给出的视角按该角色的设定合理推演，"
                "确保各视角是同一个角色、同一套服装")
    if mode == "multi":
        return ("；参考图是同一角色的多张分视角图：各视角的形象与服装与其对应的"
                "参考图保持一致，参考图未覆盖的视角按该角色的设定合理推演补全")
    return ""


def _ref_clause_single_view(mode: str) -> str:
    """单图输出（模板单图 / 普通编辑）按参考图形态给的分句。"""
    if mode == "multi":
        return ("；参考图是多张同一角色的图：以正面全身那张为主输出单张立绘，"
                "其余参考仅用于核对服装与配色细节")
    if mode == "single":
        return ("；若参考图是多视角拼接的角色设定稿，"
                "只保留其中一个视角（通常是正面），去掉其余视角的拼接")
    return ""


# 维度展示顺序（预览面板 chip 与变化短语列表共用；v0.14.0 提取为常量单一来源）
# v0.19.0：新增「画风」维度，排在构图之后（先定风格，再定穿什么/在哪）
# v0.32.0：新增「鞋袜」维度，紧跟服装（鞋袜跟随穿搭家族派生）
# v0.33.0：新增「帽子外套」维度（摘掉为主偶尔戴帽；文生图剥离对应外观 tag）
DIM_ORDER = (("构图视图", "compose"), ("构图取景", "framing"),
             ("画风", "artstyle"), ("服装", "outfit"),
             ("鞋袜", "footwear"), ("头饰", "headwear"), ("外套", "outerwear"),
             ("背景与光照", "background"), ("姿势动作", "pose"),
             ("表情", "expression"), ("体型", "body"))


def variation_zh_list(picks: dict) -> list:
    """本轮要改的中文维度短语（跳过「保持不变」的空项），LLM 元提示用。"""
    out = []
    for label, tag in DIM_ORDER:
        zh = str((picks.get(tag) or {}).get("zh") or "").strip()
        if zh:
            out.append((label, zh))
    return out


def describe_variations(seed_int: int, theme_id: str = "", outfit: str = "",
                        dim_state: dict | None = None,
                        force_single: bool = False, keep=()) -> list:
    """复算本轮变化维度（与 build_generation_prompt 同种子同口径），随档记录用。

    返回 [(维度中文标签, 中文短语)]，只含非空项。v0.22.4 起「非模板主题一律
    强制单图」不再由调用方按 strategy_type 传 force_single——base/lora 文生图
    链路此前漏传，随档维度与提示词会错位（记录里三视图、实际按单图出）。
    force_single 参数保留兼容旧调用，内部统一按主题判断。纯函数。
    v0.35.0：keep = 人设固装维度集合（keep_dims_of(entry)），与提示词生成
    同口径——否则随档维度会记出「摘掉帽子」而实际提示词里没有。
    """
    theme, _ = resolve_theme(theme_id, seed_int)
    picks = pick_variations(seed_int, theme, outfit, keep)
    if not (theme and theme.get("compose_kw")):
        picks = force_single_view(picks)
    if dim_state:
        picks = apply_dim_state(picks, seed_int, dim_state, theme, keep)
    return variation_zh_list(picks)


# ---------- 本地模板（无 LLM 兜底） ----------

def build_local_instruction(entry: dict, picks: dict, custom: str = "",
                            ref_mode: str = "") -> str:
    """img2img 用：本地拼一条 Qwen-Image-2.1 风格的自然语言编辑指令。

    结构遵循官方指南「动作 + 目标 + 约束」：先列改动，句尾统一强调保持不变。
    custom 为用户在预览面板自填的附加要求，作为一条改动子句并入。
    ref_mode：参考图形态 none/single/multi（v0.29.4）；空串 = 按 entry 自动判定。
    无参考图时不写「根据参考图」、不带多视角源收敛条款；多张参考图时写明
    「各视角与对应参考图一致 / 以正面全身那张为主」。
    """
    parts = []
    for label, zh in variation_zh_list(picks):
        if label == "构图视图":
            parts.append(f"把构图改为{zh}")
        elif label == "构图取景":
            # v0.25.7：取景决定人物在画面里占多大、裁到哪，必须和姿势分开说
            parts.append(f"把画面取景改为{zh}，人物大小随取景调整")
        elif label == "画风":
            # v0.19.0：画风是整体观感，指令里单独点明「面部与发型保持可辨认」
            # （句尾虽已统一强调，但画风类改动最容易把脸画丢，这里再锚一次）
            parts.append(f"把整体画风改为{zh}（人物面部与发型保持可辨认）")
        elif label == "服装":
            parts.append(f"把角色的服装换成{zh}")
        elif label == "鞋袜":
            # v0.32.0：鞋袜跟随穿搭（danbooru legwear/footwear 两族逐项点名）
            parts.append(f"把鞋袜改为{zh}")
        elif label == "头饰" or label == "外套":
            # v0.34.1：头饰/外套分离——zh 本身是祈使短语（「摘掉帽子」/
            # 「脱掉外套」/「戴上贝雷帽」/「换上防晒开衫」），直接作为子句；
            # 文生图侧按维度分别剥离对应外观 tag
            parts.append(zh)
        elif label == "背景与光照":
            # v0.37.0：编辑模型默认保留原图的光线与色调——背景条目自带
            # light 光照句（v0.36.0 光线驱动组）时，把「光照」写成显式改动
            # 目标，只说换地点光不会跟着变
            li = str((picks.get("background") or {}).get("light")
                     or "").strip().strip("。")
            if li:
                parts.append(f"把背景改为{zh}，整体光照与色调也随之改为{li}")
            else:
                parts.append(f"把背景改为{zh}")
        elif label == "姿势动作":
            parts.append(f"让角色{zh}")
        elif label == "表情":
            parts.append(f"表情改为{zh}")
        elif label == "体型":
            parts.append(f"把体型调整为{zh}（面部特征保持可辨认）")
    extra = str(custom or "").strip().strip("。，,")
    if extra:
        parts.append(extra)
    # v0.13.0：三/四视图模板不能被句尾的「单人竖版立绘」拉回单图——
    # 图生图指令必须明确要求同一张图内并列多个视角。
    compose_zh = str((picks.get("compose") or {}).get("zh") or "")
    multi_view = ("三视图" in compose_zh) or ("四视图" in compose_zh)
    framing_zh = str((picks.get("framing") or {}).get("zh") or "").strip()
    tail = "。保持人物的面部特征、发型发色与角色辨识度不变，只做上述改动；"
    # v0.25.7：尾巴 / 兽耳 / 角 / 翅膀这类「非人类部件」是 img2img 最容易洗掉的
    # 东西（用户实测「有尾巴的角色出图尾巴没了」）。笼统说「保持辨识度」不够，
    # 必须逐项点名。
    feat_zh = identity_feature_text(entry, "zh")
    if feat_zh:
        tail += (f"角色固有的「{feat_zh}」必须原样保留、清晰可见，"
                 f"不得丢失、缩减或改写成别的部件；")
    # v0.13.3：非多视图指令必须显式处理「源是多视图设定稿」的情况——
    # 编辑类模型（Qwen 2.1 Edit）会强烈复刻输入图布局，只说「输出单人
    # 完整竖版立绘」拉不回单图，必须写明「只保留一个视角、去掉拼接」。
    # v0.19.10：防「双头/双人」（用户实测：趴桌打瞌睡 + 全身立绘 → 桌上一颗
    # 睡着的头 + 原姿势的完整人又画了一遍）。姿势类改动必须写明「新姿势是
    # 唯一姿势」；所有单图指令都强调画面里只有这一个人物。
    pose_zh = str((picks.get("pose") or {}).get("zh") or "").strip()
    if pose_zh and not multi_view:
        tail += f"姿势以「{pose_zh}」为唯一姿势，不要保留原姿势；"
    # v0.29.4：参考图形态分流——多视图模板按「单图源=扩展推演 / 多图源=视角
    # 对齐参考图」给不同的约束；单图输出按「多图源=以正面全身为主 / 单图源=
    # 保留多视角源收敛条款 / 无图=不提参考图」。
    mode = ref_mode or template_ref_mode(entry)
    tail += ("在同一张图内并列展示上述各个视角，输出角色设定稿"
             + _ref_clause_multi_view(mode)
             if multi_view
             else "画面中只出现这一个人物、只保留一颗头部，输出单人竖版立绘"
                  + (f"（{framing_zh}）" if framing_zh else "")
                  + _ref_clause_single_view(mode))
    if not parts:
        keep = ("，保持人物的面部特征、发型发色与角色辨识度不变"
                + (f"，角色固有的「{feat_zh}」必须原样保留" if feat_zh else ""))
        if mode == "none":
            return ("直接生成该角色的单人竖版立绘"
                    + (f"（{framing_zh}）" if framing_zh else "")
                    + "（画面中只出现这一个人物）" + keep)
        return ("根据参考图重新绘制该角色的单人竖版立绘"
                + (f"（{framing_zh}）" if framing_zh else "")
                + "（画面中只出现这一个人物"
                + _ref_clause_single_view(mode)
                + "）" + keep)
    return "，".join(parts) + tail


# 视线 tag（v0.36.0）：此前 danbooru 底稿硬编码 looking at viewer——与回眸 /
# 闭眼 / 移开视线的姿势表情直接打架（「闭眼享受阳光」却盯着镜头），也是
# 「每张都盯着镜头」同质化的推手之一。默认仍看镜头（立绘惯例），姿势或
# 表情自带视线语义时让位。
_GAZE_AWAY_KWS = ("looking back", "from behind", "closed eyes", "looking away",
                  "sleeping", "side view", "back view", "looking down",
                  "looking into distance", "looking afar")


def _gaze_tag(picks: dict) -> str:
    """按姿势/表情派生视线 tag（纯函数）：默认 looking at viewer，视线类
    姿势/表情在场时返回空串（视线交给它们自己表达）。"""
    for k in ("pose", "expression"):
        en = str((picks.get(k) or {}).get("en") or "").lower()
        if any(w in en for w in _GAZE_AWAY_KWS):
            return ""
    return "looking at viewer"


def build_t2i_prompt(entry: dict, picks: dict) -> str:
    """danbooru 系底模（anima/illustrious/noobai/pony 等）用：tags 拼装 + 变化 tag。

    v0.19.0：画风 tag 紧跟质量词（booru 惯例：质量/风格 → 角色 → 其余），
    这样底模更容易把整张图往该画风上拉。
    v0.33.0：帽子外套维度为「摘掉/更换」时，先从角色 tags 里剥掉 hat/jacket
    等外观 tag（否则底模按 tag 照画，主题换装也去不掉）。
    v0.36.0：looking at viewer 不再硬编码——按 _gaze_tag 派生。
    """
    base = "1girl, solo, best quality"
    gaze = _gaze_tag(picks)
    if gaze:
        base += f", {gaze}"
    tags = _entry_tags(entry, picks)
    extra = []
    # v0.25.7：取景紧跟质量词；姿势 tag 里与取景矛盾的 "full body" 要摘掉
    # （否则「upper body」+「full body」同串，底模会按后者画全身）
    framing_en = str((picks.get("framing") or {}).get("en") or "").strip().strip(",")
    if framing_en:
        extra.append(framing_en)
    for tag in ("artstyle", "compose", "outfit", "footwear", "headwear", "outerwear",
                "background", "pose", "expression", "body"):
        en = str((picks.get(tag) or {}).get("en") or "").strip().strip(",")
        if tag == "pose" and en and framing_en and "full body" not in framing_en:
            en = re.sub(r",\s*,", ",", en.replace("full body", "")).strip(", ")
        if en:
            extra.append(en)
    segs = [base]
    if tags:
        segs.append(tags)
    if extra:
        segs.append(", ".join(extra))
    return ", ".join(segs)


def build_t2i_natural_local(entry: dict, picks: dict, custom: str = "") -> str:
    """自然语言系底模（Moss-Image/FLUX/krea 等）用：英文 caption 本地兜底。

    按 MOSS-Image 规范的 Tag 串顺序拼（Quality → Character/Series → General）：
    MOSS 三式混用皆可，本地不写长句是保底——真 caption 交给 LLM 通道。
    """
    # v0.25.7：不再硬编码 full body portrait——取景交给 framing 维度
    segs = ["best quality, highres, solo"]
    # v0.19.0：画风 tag 紧跟质量词，再上取景、角色 tags、其余维度
    az = str((picks.get("artstyle") or {}).get("en") or "").strip().strip(",")
    if az:
        segs.append(az)
    framing_en = str((picks.get("framing") or {}).get("en") or "").strip().strip(",")
    if framing_en:
        segs.append(framing_en)
    tags = _entry_tags(entry, picks)
    if tags:
        segs.append(tags)
    for tag in ("compose", "outfit", "footwear", "headwear", "outerwear", "background",
                "pose", "expression", "body"):
        en = str((picks.get(tag) or {}).get("en") or "").strip().strip(",")
        if tag == "pose" and en and framing_en and "full body" not in framing_en:
            en = re.sub(r",\s*,", ",", en.replace("full body", "")).strip(", ")
        if en:
            segs.append(en)
    feats = identity_feature_text(entry, "en")
    if feats:
        segs.append(f"keep {feats} clearly visible")
    extra = str(custom or "").strip().strip(",")
    if extra:
        segs.append(extra)
    return ", ".join(segs)


def build_t2i_qwen_local(entry: dict, picks: dict, custom: str = "") -> str:
    """Qwen-Image-2.1 文生图用：按官方黄金公式拼的本地兜底短版。

    官方指南给的顺序是「媒介 → 主体 → 动作 / 关系 → 构图 → 光照 → 材质」，
    一段连贯自然语言、每个属性紧跟它修饰的对象、光照单独成句、只描述成品而
    不写「请画/帮我生成」这类指令（v0.25.7 按该口径重排；真长段仍交给 LLM）。
    """
    name = str(entry.get("name") or "").strip() or "该角色"
    az = str((picks.get("artstyle") or {}).get("zh") or "").strip().strip("。")
    framing_zh = str((picks.get("framing") or {}).get("zh") or "").strip().strip("。")
    # ① 媒介 + 画风（首句定框架）
    medium = f"一幅{az or '现代日式动画风格'}的单人角色立绘插画"
    # ② 主体：角色 + 固有特征逐项点名（尾巴/兽耳/角这些最容易被省略）
    subj = f"画面主体是{name}"
    feats = identity_feature_text(entry, "zh")
    if feats:
        subj += f"，角色的{feats}清晰可见、不可省略"
    tags = _entry_tags(entry, picks)
    if tags:
        subj += f"，外观参照这些特征：{tags}"
    # ③ 动作 / 服装 / 鞋袜 / 帽子外套 / 表情
    acts = [str((picks.get(k) or {}).get("zh") or "").strip().strip("。")
            for k in ("pose", "outfit", "footwear", "headwear", "outerwear", "expression",
                      "body")]
    acts = [s for s in acts if s]
    if acts:
        subj += "，" + "；".join(acts)
    # ④ 构图：视图数 + 取景
    if _compose_is_multi(picks):
        comp = "同一张图内并列展示正面、侧面、背面的角色设定稿"
    else:
        comp = "单人画面、只保留一个头部" + (f"，{framing_zh}" if framing_zh else "")
    # ⑤ 背景 ⑥ 光照（单独成句）
    bg_pick = picks.get("background") or {}
    bg = str(bg_pick.get("zh") or "").strip().strip("。")
    # v0.36.0：光照句不再每张都一样——光线驱动类背景条目自带 light 字段
    # （光的方向 / 时间感 / 色调），无 light 的条目回落通用光句。
    light = (str(bg_pick.get("light") or "").strip().strip("。")
             or "光照自然通透，主光柔和、边缘有一层淡轮廓光，明暗过渡细腻")
    segs = [medium, subj, comp]
    if bg:
        segs.append(bg)
    segs.append(light)
    extra = str(custom or "").strip().strip("。")
    if extra:
        segs.append(extra)
    segs.append("整体干净利落、细节精致，画面里不出现第二个人物")
    return "。".join(s.strip("。") for s in segs if str(s).strip()) + "。"


# ---------- LLM 通道 ----------

_SYSTEM_PROMPT = (
    "你为 Qwen-Image-2.1（通义千问图像模型，文生图与图像编辑统一在同一模型，"
    "编辑由自然语言指令驱动）撰写图生图编辑指令。"
    "写法要点：一句话内「动作+目标+约束」，句尾强调保持人物面部特征、"
    "发型发色与角色辨识度不变。"
    "改动子句必须全部保留、一条都不能丢——改动很多时按「人物 → 服装 → "
    "场景 → 氛围」排序，逐条写清，绝不合并省略（丢子句 = 那个维度这轮白改）。"
    "若给定了「角色固有特征」（尾巴、兽耳、角、翅膀、光环等），必须逐项写明"
    "「原样保留、清晰可见、不得丢失或改写」——编辑模型最容易把这些非人类"
    "部件洗掉，笼统的「保持辨识度」不够。"
    "若改动维度里有「画风」，把它写成「把整体画风改为…（人物面部与发型保持可辨认）」，"
    "画风类改动最容易把脸画丢，必须点明保持辨识度。"
    "若改动维度里有「构图取景」，明确写出裁切范围（半身 / 胸像特写 / 膝上 /"
    " 全身），不要默认全身；取景与姿势是两件事，分开表述。"
    "画面氛围（必须落实）：编辑模型默认保留原图的光线与色调——背景子句里"
    "括号内的光线描述（光源方向、光斑、空气感）必须一并写进指令，让整张图的"
    "光影与新场景一致；只换地点不换光，画面就会发闷。"
    "画面纪律（必须写进指令）：画面中只允许出现这一个人物、只保留一颗头部；"
    "改动姿势时以新姿势为唯一姿势，绝不能把原姿势与新姿势同时画出来"
    "（两个头/两个身体/分身都是严重错误）。"
    "输出纪律：只输出一条简体中文编辑指令本身——不要解释、不要引号、"
    "不要 markdown、不要分点，总长不超过 300 字。"
)

# 文生图（natural 系底模，如 Moss-Image）：MOSS-Image 规范 A 档——英文 caption、
# 单主体一段描述、约 20~60 词、禁质量词堆砌、身份锚定逐项写明。
_SYSTEM_T2I_NATURAL = (
    "你为 Qwen-Image-2.1 / Moss-Image 等自然语言系文生图模型撰写单人角色"
    "立绘的英文提示词。写法：一段连贯英文 caption，按「媒介与画风 → 主体与"
    "固有特征 → 动作姿势 → 服装 → 构图取景 → 背景与光照 → 材质细节」的顺序展开，"
    "描述成品画面而不是下指令（不要写 please draw / generate）。"
    "输出必须是 solo, single character（画面里只有这一个人物）。"
    "角色身份锚定：把已给 tags 里的发色/发型/瞳色/标志性特征自然融入描述。"
    "角色固有特征以提示中给出的「必须保留的固有特征」字段为准：有则单独点名"
    "（tail / animal ears / horns / wings / halo 等）必须清晰可见、不可省略"
    "——这类部件最容易在重绘时消失；字段未给出时严禁凭空添加 tail / animal "
    "ears / horns / wings / halo 等任何非人类部件，人物特征只以已给 tags 为准。"
    "构图取景按给定维度写（close-up / portrait / cowboy shot / full body），"
    "未给定时优先半身或胸像，不要默认 full body。"
    "背景与光照必须写具体（这是画面氛围的来源，不要写成空荡的白底）：光源方向"
    "与时间感（backlighting / dappled sunlight / golden hour / neon…）、空气感"
    "（light particles / lens flare / depth of field）、整体色调倾向，让光成为"
    "画面的一部分；若姿势/表情与视线冲突（looking back / closed eyes 等），"
    "删掉 looking at viewer，视线服从动作。"
    "画风维度翻成对应英文风格词（modern anime illustration / cel shading / "
    "cinematic anime / watercolor…），整段保持同一画风。"
    "禁止：8k/highly detailed 之类质量词堆砌（最多以 best quality, highres 开头）、"
    "markdown、解释性文字。"
    "输出纪律：只输出提示词本体，一段话，40~90 词，不超过 140 词。"
)

# 文生图（Qwen-Image-2.1）：官方黄金公式——媒介与画风 → 主体 → 动作 → 服装 →
# 构图取景 → 背景 → 光照 → 材质，一段连贯中文描述，光照单独成句，只描述成品。
_SYSTEM_T2I_QWEN = (
    "你为 Qwen-Image-2.1 撰写单人角色立绘的文生图提示词。严格按官方黄金公式"
    "的顺序写：媒介与画风 → 主体（角色名 + 外观 + 固有特征）→ 动作与姿势 →"
    "服装 → 构图取景 → 背景环境 → 光照 → 材质与细节；每个属性紧跟它修饰的"
    "对象，写成一段连贯中文描述（6~12 句），首句先定框架。"
    "必须遵守：① 角色固有特征以提示中给出的「必须保留的固有特征」字段为准："
    "有则逐项写进主体描述并强调「清晰可见、不可省略」；字段未给出时严禁凭空"
    "添加尾巴、兽耳、角、翅膀、光环等任何非人类部件（人物特征只以 tags 为准）；"
    "② 光照单独成句，写足画面氛围三件套——光（方向/时间感/光斑粒子）、"
    "环境层次（前后景元素与空气感）、整体色调倾向；背景与光照是画面「有生活」"
    "的关键，不要写成空荡的纯色底；③ 构图取景按"
    "给定维度写（半身 / 胸像特写 / 膝上 / 全身），未给定时优先半身或胸像，"
    "不要默认全身；④ 只描述成品画面，绝不写「请画」「帮我生成」这类指令；"
    "⑤ 画面中只有这一个人物、只保留一颗头部，不出现第二个人；"
    "⑥ 不写比例数字、不写 best quality 之类画质套话、不写标签串、不用 markdown；"
    "⑦ 视线服从动作——姿势或表情是回眸 / 闭眼 / 移开视线时，不要写看镜头。"
    "输出纪律：只输出提示词本体一段话，不要解释、不要引号、不要分点。"
)


def build_meta_prompt(entry: dict, picks: dict, custom: str = "",
                      ref_mode: str = "") -> str:
    name = str(entry.get("name") or "").strip() or "该角色"
    series = str(entry.get("series") or "").strip() or "未知作品"
    # v0.33.0：帽子外套为「摘掉/更换」时剥掉对应外观 tag，避免元提示词里的
    # tags 与维度指令（摘掉帽子）自相矛盾
    tags = _entry_tags(entry, picks) or "无"
    # v0.29.4：参考图形态分流（none/single/multi）。旧版对所有情况都说
    # 「若参考图是多视角设定稿必须收敛为单张立绘」——三/四视图模板撞上这句
    # 就是自相矛盾（前面要求并列多视角、后面要求收敛单图）；无参考图时更是
    # 凭空谈论不存在的参考图。按形态给准确的话：
    #   多视图模板 + 单图源 → 以单图为基准扩展推演其余视角；
    #   多视图模板 + 多图源 → 各视角与对应参考图一致；
    #   单图输出   + 多图源 → 以正面全身那张为主，其余仅核对细节；
    #   单图输出   + 单图源 → 保留「多视角源收敛为单图」条款（防复刻拼接布局）；
    #   无参考图           → 不提参考图。
    mode = ref_mode or template_ref_mode(entry)
    if _compose_is_multi(picks):
        ref_para = ("参考图只有一张（通常是正面）：以参考图中的角色形象与服装为"
                    "基准，在同一张图内并列补全其余视角；参考图未给出的视角按"
                    "该角色的设定合理推演，确保各视角是同一个角色、同一套服装。"
                    if mode == "single" else
                    "参考图是同一角色的多张分视角图：各视角的形象与服装与其对应的"
                    "参考图保持一致；参考图未覆盖的视角按该角色的设定合理推演补全。"
                    if mode == "multi" else
                    "本轮没有参考图：直接按角色 tags 与设定生成并列多视角的"
                    "角色设定稿。")
    elif mode == "multi":
        ref_para = ("参考图是同一角色的多张图：以正面全身那张为主输出单张立绘，"
                    "其余参考仅用于核对服装与配色细节；若某张参考图本身是多视角"
                    "拼接的角色设定稿，只取其中一个视角（通常是正面）。")
    elif mode == "single":
        # v0.13.3：源图不再硬编码「单人立绘」——生成源可能是多视图设定稿，
        # 硬编码会让 LLM 写不出「转单图」指令，输出复刻设定稿布局（实测）。
        ref_para = ("参考图是该角色的一条生成源：可能是单张立绘，也可能是多视角"
                    "拼接的角色设定稿；若参考图是多视角设定稿，输出必须收敛为"
                    "单张立绘（保留其中一个视角，通常是正面），不要保留拼接布局。")
    else:
        ref_para = "本轮没有参考图：直接按角色 tags 与设定生成。"
    lines = [
        f"角色：{name}（出自 {series}），特征 tags：{tags}。",
        # v0.25.7：尾巴/兽耳/角/翅膀这类部件必须在指令里逐项点名保留
        (f"必须保留的固有特征：{identity_feature_text(entry, 'zh')}"
         "（原样保留、清晰可见，不得丢失或改写）。"
         if identity_feature_text(entry, "zh") else ""),
        ref_para + "本轮由抽签决定的改动维度：",
    ]
    zh_list = variation_zh_list(picks)
    if zh_list:
        lines += [f"- {label}：{zh}" for label, zh in zh_list]
    else:
        lines.append("- （全部维度保持不变）")
    extra = str(custom or "").strip()
    if extra:
        lines.append(f"- 用户附加要求：{extra}")
    framing_zh = str((picks.get("framing") or {}).get("zh") or "").strip()
    goal = ("角色是二次元角色；本轮要求在同一张图内并列展示正面、侧面、背面等"
            "多个视角，输出角色设定稿，不要退化成单张立绘。"
            if _compose_is_multi(picks)
            else "角色是二次元角色，目标是单张立绘"
                 + (f"（{framing_zh}）" if framing_zh
                    else "，构图取景按上面给定的维度")
                 + "；画面中只允许出现一个人物、一颗头部，改动姿势时以新姿势为"
                   "唯一姿势，不要同时呈现改动前后的两个状态。")
    lines += [
        "请把以上改动自然融合成一条编辑指令；未列出的维度一律不要改动。",
        goal,
    ]
    return "\n".join(lines)


def sanitize_llm_output(text: str, limit: int = 150) -> str:
    """LLM 返回清洗：剥 markdown/引号/前缀废话，取首个非空行，限长。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"```[a-zA-Z]*", "", s).replace("```", "")
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        s = line
        break
    s = s.strip("\"'“”「」『』《》`*_ ")
    s = re.sub(r"^(编辑指令|指令|提示词|prompt)\s*[:：]\s*", "", s, flags=re.I)
    # 前缀剥掉后开头可能又露出引号（如「编辑指令：“…”」），再 strip 一次
    s = s.strip("\"'“”「」『』《》`*_ ")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if s else ""


async def _resolve_provider(context, provider_id: str):
    """按配置解析对话模型实例：显式 ID → 默认提供商 → 任一 chat provider。"""
    pid = str(provider_id or "").strip()
    if pid:
        try:
            p = context.get_provider_by_id(pid)
        except Exception:
            p = None
        if p is not None and hasattr(p, "text_chat"):
            return p
        return None
    try:
        p = await context.get_using_provider_async()
        if p is not None and hasattr(p, "text_chat"):
            return p
    except Exception:
        pass
    try:
        provs = context.get_all_providers() or []
    except Exception:
        provs = []
    for p in provs:
        if hasattr(p, "text_chat"):
            return p
    return None


def effective_workflow(entry: dict, cfg: dict) -> str:
    """本次出图**实际会用的**文生图工作流名：条目 strategy.workflow > 全局。

    与 draw.generate_portrait / _base_kwargs 的解析顺序保持一致——风格探测
    必须跟着真正出图的工作流走，否则「试画台/补一张选了 Qwen 工作流、
    提示词却按全局（另一个底模）拼成 tags」这种错位就会发生（v0.29.7 实测）。
    """
    st = entry.get("strategy") or {}
    wf = str(st.get("workflow") or "").strip() if isinstance(st, dict) else ""
    return wf or str(cfg.get("draw_workflow") or "").strip()


def detect_t2i_style(context, workflow_name: str) -> str:
    """探测文生图工作流底模的提示词风格（danbooru / natural / qwen）。

    链路：萌绘 star_cls.config["workflows"] 找 workflow_name 条目 → base_id →
    workflow_store.get(base_id)["roles"]["model_file"] → basemodel_store.
    match_model(model_file)["prompt_style"]。
    v0.29.7：① 底模库没建档 / 没填风格时，退而按模型文件名直接猜
    （含 qwen → qwen、含 moss → natural），不再一律砸回 danbooru——用户
    「主力 Qwen Image 2.1」却满屏标签串就是这么来的；② 每次探测留 info 日志
    （工作流 → 底模 → 风格），「为什么变标签串」从此有据可查。
    """
    wf_name = str(workflow_name or "").strip()
    try:
        getter = getattr(context, "get_registered_star", None)
        if getter is None:   # 无 astrbot 环境（测试桩等），静默回落
            return "danbooru"
        meta = getter("astrbot_plugin_comfyui_anima")
        anima = getattr(meta, "star_cls", None)
        if anima is None:
            logger.info("[moe_waifu] 提示词风格：未安装萌绘，按 danbooru tags")
            return "danbooru"
        cfg = getattr(anima, "config", None)
        raw = cfg.get("workflows") if isinstance(cfg, dict) else None
        wf = next((e for e in (raw or [])
                   if isinstance(e, dict)
                   and str(e.get("name") or "") == wf_name
                   and str(e.get("base_id") or "").strip()), None)
        if wf is None:
            logger.info("[moe_waifu] 提示词风格：工作流「" + wf_name
                        + "」未绑基础工作流库（旧版模式），按 danbooru tags")
            return "danbooru"
        store = getattr(anima, "workflow_store", None)
        roles = ((store.get(int(wf["base_id"])) or {}).get("roles") or {}) \
            if store is not None else {}
        model_file = str(roles.get("model_file") or "")
        bs = getattr(anima, "basemodel_store", None)
        rec = bs.match_model(model_file) if bs is not None and model_file else None
        style = str((rec or {}).get("prompt_style") or "").strip().lower()
        if style in ("danbooru", "natural", "qwen"):
            logger.info("[moe_waifu] 提示词风格：工作流「" + wf_name
                        + "」→ 底模「" + (model_file or "?") + "」→ " + style)
            return style
        # 底模库没建档 / 没填风格：按模型文件名猜一版，别一律砸回 tags
        mf = model_file.lower()
        if "qwen" in mf:
            style = "qwen"
        elif "moss" in mf:
            style = "natural"
        else:
            style = "danbooru"
        logger.info("[moe_waifu] 提示词风格：底模「" + (model_file or "?")
                    + "」未在萌绘底模库标注风格，按文件名猜为 " + style
                    + "（要精确请在萌绘底模库里给该底模设提示词风格）")
        return style
    except Exception as exc:
        logger.warning("[moe_waifu] 提示词风格探测异常（" + wf_name
                       + "）：" + repr(exc) + "，按 danbooru tags")
        return "danbooru"


def build_t2i_meta_prompt(entry: dict, picks: dict, style: str,
                          custom: str = "") -> str:
    name = str(entry.get("name") or "").strip() or "该角色"
    series = str(entry.get("series") or "").strip()
    # v0.33.0：帽子外套为「摘掉/更换」时剥掉对应外观 tag
    tags = _entry_tags(entry, picks) or "无"
    extra = str(custom or "").strip()
    feat_zh = identity_feature_text(entry, "zh")
    feat_en = identity_feature_text(entry, "en")
    if style == "qwen":
        head = f"角色：{name}" + (f"（出自 {series}）" if series else "")
        segs = [f"{head}。外观特征 tags（供参考，需转写成自然语句）：{tags}。"]
        if feat_zh:
            segs.append(f"必须逐项写进主体描述的固有特征：{feat_zh}"
                        "（强调清晰可见、不可省略）。")
        segs += [f"{label}：{zh}" for label, zh in variation_zh_list(picks)]
        if extra:
            segs.append(f"用户附加要求：{extra}")
        segs.append(
            "目标是同一张图内并列展示多视角的角色设定稿，请按官方公式"
            "（媒介与画风 → 主体 → 动作 → 服装 → 构图 → 背景 → 光照 → 材质）"
            "写成一段连贯中文长描述。"
            if _compose_is_multi(picks)
            else "目标是单张角色立绘（构图取景以上面的维度为准，不要默认全身），"
                 "请按官方公式（媒介与画风 → 主体 → 动作 → 服装 → 构图 → 背景 →"
                 " 光照 → 材质）写成一段连贯中文长描述。")
        return "\n".join([s for s in segs if s])
    # natural（MOSS 口径）
    head = f"Character: {name}" + (f" ({series})" if series else "")
    segs = [f"{head}. Identity tags (anchor these features verbatim): {tags}."]
    if feat_en:
        segs.append(f"Must appear and stay clearly visible: {feat_en}.")
    segs += [f"{label}: {zh}" for label, zh in variation_zh_list(picks)]
    if extra:
        segs.append(f"User extra requirement: {extra}")
    segs.append("Target: one single-character illustration; framing follows the "
                "given composition dimension (do not default to full body). "
                "Write ONE connected English caption.")
    return "\n".join([s for s in segs if s])


def _t2i_local_of(style: str, entry: dict, picks: dict, custom: str = "") -> tuple:
    """分风格的文生图本地兜底 + 静态回退。"""
    if style == "natural":
        return build_t2i_natural_local(entry, picks, custom), "local"
    if style == "qwen":
        return build_t2i_qwen_local(entry, picks, custom), "local"
    return build_t2i_prompt(entry, picks), "local"


async def build_generation_prompt(context, cfg: dict, entry: dict,
                                  seed_int: int, theme_id: str = "",
                                  custom: str = "", outfit: str = "",
                                  dim_state: dict | None = None) -> tuple:
    """产出生图提示词。返回 (prompt, source)；source ∈ llm / local / static。

    绝不抛异常——LLM 链路任何失败都回落本地模板（提示词不是阻塞出卡的理由）。
    img2img（qwen image 2.1 编辑）走自然语言编辑指令；base/lora（文生图）按
    工作流底模风格分派：danbooru=tag 拼装、natural=英文 caption、qwen=中文
    长描述（风格探测失败一律回落 danbooru tag 拼装）。
    theme_id：主题预设（random = 从内容主题池按种子摇一个，v0.20.0 起；
    空串 = 抽卡主链路现状：不套主题、维度纯随机）。
    custom：用户自填的附加要求（融入指令/描述）。
    outfit：用户指定穿搭（预览面板「穿搭」框，空 = 按种子/主题随机）。
    dim_state：预览面板维度级控制（v0.14.0，见 apply_dim_state；抽卡主链路
    不传 = 纯种子决定论不变）。
    v0.35.0：角色条目的 keep_xxx（人设固装）在这里统一换算成 keep 集合并传给
    pick_variations / apply_dim_state——文生图、图生图与异常回落三条路全部
    同口径，随机不再产出「摘掉帽子」类违背人设的指令。
    """
    theme, _theme_rec = resolve_theme(theme_id, seed_int)
    # v0.31.1：tpl_ 模板主题 = 固定底稿（v0.29.3 口径：模板是固定提示词，不能充满
    # 随机性），**跳过 LLM 通道**——LLM 每次改写措辞是非确定源，实测三视图模板
    # 「有时候」被压缩掉关键约束（并列设定稿强调 / 参考图对齐条款），文生图 qwen
    # 通道甚至会写出「把构图改为…」的编辑指令口吻。模板提示词必须逐位确定：
    # 图生图走 build_local_instruction，文生图走 _t2i_local_of（含 qwen/natural）。
    is_tpl = bool(theme and str(theme.get("id") or "").startswith("tpl_"))
    keep = keep_dims_of(entry)
    try:
        stype = str(entry.get("strategy_type") or "base")
        if stype == "img2img" and template_ref_mode(entry) == "none":
            # v0.29.4：与 draw.generate_portrait 的降级口径一致——img2img 却
            # 没有显式参考图时按 tags 文生图；提示词侧若仍发「把构图改为…」的
            # 编辑指令，就是「指令说改图、实际无图可改」两边打架。
            # v0.29.9：降级留痕——此前静默，「为什么提示词像文生图、不提参考图」
            # 无据可查（实报案例即前端预览没把已选生成源传进 override）。
            logger.info("[moe_waifu] img2img 条目没有显式参考图源，"
                        "提示词按文生图口径生成（要在指令里改参考图，"
                        "请先选定生成源再画）")
            stype = "base"
        if stype != "img2img":
            if not bool(cfg.get("variations_enabled", True)):
                return build_prompt_tags(entry), "static"
            picks = pick_variations(seed_int, theme, outfit, keep)
            # v0.22.4：文生图同样不许随机混入三/四视图（此前只有 img2img
            # 分支做了，base/lora 吐出设定稿就是这么来的）。tpl_ 模板主题
            # 带 compose_kw，仍可显式出多视角设定稿。
            if not (theme and theme.get("compose_kw")):
                picks = force_single_view(picks)
            picks = apply_dim_state(picks, seed_int, dim_state, theme, keep)
            # v0.29.7：按「实际会用的」工作流探测（条目/本次覆盖 > 全局），
            # 修「选了 Qwen 工作流却按全局底模拼 tags」的错位
            style = detect_t2i_style(context, effective_workflow(entry, cfg))
            if (not bool(cfg.get("prompt_llm_enabled", True)) or style == "danbooru"
                    or is_tpl):
                return _t2i_local_of(style, entry, picks, custom)
            provider = await _resolve_provider(
                context, cfg.get("prompt_llm_provider", ""))
            if provider is None:
                logger.warning("[moe_waifu] 未找到可用的对话模型，提示词回落本地模板")
                return _t2i_local_of(style, entry, picks, custom)
            timeout = int(cfg.get("prompt_llm_timeout", 30) or 30)
            if timeout <= 0:
                timeout = 30
            system = _SYSTEM_T2I_QWEN if style == "qwen" else _SYSTEM_T2I_NATURAL
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=build_t2i_meta_prompt(entry, picks, style, custom),
                        session_id=None,
                        system_prompt=system,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[moe_waifu] 提示词 LLM 超时（>{timeout}s），回落本地模板")
                return _t2i_local_of(style, entry, picks, custom)
            # v0.36.0：限额放宽（qwen 400→650 / natural 300→450）——system prompt
            # 现在要求写足氛围三件套，旧限额会把 LLM 写好的光照与层次又掐掉。
            text = sanitize_llm_output(getattr(resp, "completion_text", "") or "",
                                       limit=650 if style == "qwen" else 450)
            if text:
                return text, "llm"
            logger.warning("[moe_waifu] 提示词 LLM 返回为空，回落本地模板")
            return _t2i_local_of(style, entry, picks, custom)
        if not bool(cfg.get("variations_enabled", True)):
            return build_prompt_tags(entry), "static"
        picks = pick_variations(seed_int, theme, outfit, keep)
        # v0.13.3：图生图编辑模式（非模板主题）compose 强制单视图——
        # 别让 COMPOSE 池随机命中三/四视图混进编辑指令（见 force_single_view）
        if not (theme and theme.get("compose_kw")):
            picks = force_single_view(picks)
        # v0.14.0：维度级控制在强制单图之后——面板显式启用/锁定的 compose
        # 是用户意图，应能覆盖「随机命中被拦」的默认行为
        picks = apply_dim_state(picks, seed_int, dim_state, theme, keep)
        if not bool(cfg.get("prompt_llm_enabled", True)) or is_tpl:
            # v0.31.1：is_tpl 一并短路——模板的编辑指令走本地确定性拼装
            return build_local_instruction(entry, picks, custom), "local"
        provider = await _resolve_provider(context, cfg.get("prompt_llm_provider", ""))
        if provider is None:
            logger.warning("[moe_waifu] 未找到可用的对话模型，提示词回落本地模板")
            return build_local_instruction(entry, picks, custom), "local"
        timeout = int(cfg.get("prompt_llm_timeout", 30) or 30)
        if timeout <= 0:
            timeout = 30
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=build_meta_prompt(entry, picks, custom),
                    session_id=None,
                    system_prompt=_SYSTEM_PROMPT,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[moe_waifu] 提示词 LLM 超时（>{timeout}s），回落本地模板")
            return build_local_instruction(entry, picks, custom), "local"
        # v0.37.0：限额 150→340——system 纪律放宽到 300 字（130 字装不下
        # 7 个改动子句，LLM 被迫丢子句，背景/光照/姿势在图生图里被整条砍掉）
        text = sanitize_llm_output(getattr(resp, "completion_text", "") or "",
                                   limit=340)
        if text:
            return text, "llm"
        logger.warning("[moe_waifu] 提示词 LLM 返回为空，回落本地模板")
        return build_local_instruction(entry, picks, custom), "local"
    except Exception:
        logger.error("[moe_waifu] 提示词生成异常，回落本地模板", exc_info=True)
        try:
            if str(entry.get("strategy_type") or "base") == "img2img":
                picks = pick_variations(seed_int, theme, outfit, keep)
                if not (theme and theme.get("compose_kw")):
                    picks = force_single_view(picks)
                picks = apply_dim_state(picks, seed_int, dim_state, theme, keep)
                return (build_local_instruction(entry, picks, custom), "local")
            return build_prompt_tags(entry), "static"
        except Exception:
            return "1girl, solo, best quality, looking at viewer", "static"


def build_prompt_tags(entry: dict) -> str:
    """关闭变化维度时的原样 tag 拼装（与 draw.build_prompt 等价，避免循环依赖）。"""
    base = "1girl, solo, best quality, looking at viewer"
    tags = str(entry.get("tags_en") or "").strip().rstrip(",")
    return f"{base}, {tags}" if tags else base
