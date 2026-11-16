// 图片选择弹窗组件（WImagePicker）运行时守卫：抽出组件真跑 render/toggle，
// 抓静态检查抓不到的「render 抛错 / 网格没建 / 单选多选逻辑坏」。node 直接跑。
const fs = require('fs');
const path = require('path');
const PAGE = path.join(__dirname, '..', 'pages', 'waifu-console', 'index.html');
const html = fs.readFileSync(PAGE, 'utf8');

function fail(m) { console.log('FAIL: ' + m); process.exit(1); }

// 1) 组件定义存在且已全局注册
if (html.indexOf('var WImagePicker = {') < 0) fail('WImagePicker 未定义');
if (html.indexOf('app.component("w-image-picker", WImagePicker)') < 0) fail('组件未全局注册');
const start = html.indexOf('var WImagePicker = {');
const end = html.indexOf('\n  };', start);
if (start < 0 || end < 0) fail('无法定位组件边界');
const src = html.slice(start, end + 5);

function h(tag, props, third) {
  const v = { tag, props: props || {}, children: null, slots: null };
  if (Array.isArray(third)) v.children = third;
  else if (third) v.slots = third;
  return v;
}
const NS = { NModal: 'NModal', NButton: 'NButton' };
let Comp;
try {
  Comp = (new Function('h', 'NS', 'Vue', src + '\nreturn WImagePicker;'))(h, NS, {});
} catch (e) { fail('eval 组件抛错: ' + e.message); }
// v0.60.0：弹窗必须自绘浮层，不能再用 NS.NModal（vendored naive 的 dialog preset
// 在 render function 里不转发 default/action slot → 弹窗空白）
if (/h\(NS\.NModal/.test(src)) fail('又用回 NS.NModal（会空白）');
if (html.indexOf('.picker-overlay') < 0) fail('自绘浮层样式缺失');
if (!Comp || !Comp.render) fail('组件缺 render');
if (!Comp.methods || !Comp.methods.toggle || !Comp.methods.doDelete) fail('组件缺方法');

// 2) render 真跑：多选 2 项（1 有效 1 丢失）+ 已选
const inst = {
  items: [
    { path: 'a.png', thumb: 'data:x', label: 'A', sub: '700x1200', exists: true },
    { path: 'b.png', thumb: '', label: 'B', exists: false }
  ],
  draft: ['a.png'], multiple: true, max: 0, armedDel: '', title: 'T', show: true, deletable: true
};
for (const k of ['selIndex', 'toggle', 'doPreview', 'doDelete', 'doCancel', 'doConfirm']) {
  inst[k] = Comp.methods[k].bind(inst);
}
inst.$emit = function () {};
let vnode;
try { vnode = Comp.render.call(inst); } catch (e) { fail('render 抛错: ' + e.message); }
if (!vnode || vnode.tag !== 'div' || !vnode.props || vnode.props.class !== 'picker-overlay') {
  fail('render 未返回自绘浮层 .picker-overlay');
}
const dialog = vnode.children && vnode.children.find(
  c => c && c.tag === 'div' && c.props && c.props.class === 'picker-dialog');
if (!dialog) fail('浮层内缺 .picker-dialog');
const body = dialog.children && dialog.children.find(
  c => c && c.tag === 'div' && c.props && c.props.class === 'picker-body');
if (!body || !Array.isArray(body.children) || body.children.length < 3) fail('弹窗正文缺失');
const grid = body.children.find(k => k && k.tag === 'div' && k.props && k.props.class === 'picker-grid');
if (!grid || !Array.isArray(grid.children) || grid.children.length !== 2) fail('图片网格未正确构建');
// show=false 必须不渲染浮层（否则会常驻遮挡页面）
inst.show = false;
if (Comp.render.call(inst) !== null) fail('show=false 时浮层未隐藏');
inst.show = true;

// 3) 交互逻辑：丢失图不可选、toggle 取消、单选覆盖、删除两段确认
inst.toggle({ path: 'b.png', exists: false });
if (inst.draft.length !== 1) fail('丢失图不应可选');
inst.toggle({ path: 'a.png', exists: true });
if (inst.draft.length !== 0) fail('多选取消失败');
inst.multiple = false;
inst.toggle({ path: 'a.png', exists: true });
if (inst.draft.length !== 1) fail('单选选中失败');
inst.armedDel = '';
inst.doDelete({ path: 'a.png' });
if (inst.armedDel !== 'a.png') fail('删除两段确认第一步失败');

// 4) 试画台三处都已挂上 picker
for (const tag of ['tryRefPickerOpen', 'trySwapGlobalPickerOpen', 'trySwapCharPickerOpen']) {
  if (html.indexOf('v-model:show="' + tag + '"') < 0) fail('试画台未挂 ' + tag);
}

// 5) 全局图库备注/标签走独立弹窗，不再内嵌卡片撑乱布局
if (html.indexOf('v-model:show="grNoteOpen"') < 0) fail('备注编辑弹窗缺失');
if (html.indexOf('class="gr-note-edit"') >= 0) fail('卡片里仍有内嵌备注编辑器（会撑乱布局）');
if (html.indexOf('.gr-card-note') < 0 || html.indexOf('.gr-card-cover') < 0) {
  fail('全局图库卡片新样式缺失');
}

// 6) v0.59.2：数据源必须在首屏 / 换页签 / 换画法时真的加载。
// v0.58~v0.59.1 组件本身没错，是 globalRefs 只在点「全局图库」页签时拉、
// tryRefs 只在 img2img 下拉——用户从试画台直达 swap 模式时两者恒空，弹窗空白。
const boot = html.indexOf('loadAll().then(function ()');
if (boot < 0) fail('找不到首屏加载块');
const bootSeg = html.slice(boot, boot + 800);
if (bootSeg.indexOf('return Promise.all([loadGallery()') < 0
    || bootSeg.indexOf('loadGlobalRefs()') < 0) {
  fail('首屏没拉 globalRefs（角色替换「画布」弹窗必空）');
}
if (!/else if \(m === "global_refs"\) \{ loadGlobalRefs\(\); \}/.test(html)) {
  fail('menu watcher 缺 global_refs 分支（服务端记忆页签恢复后列表不加载）');
}
const mw = html.indexOf('return [tryCharId.value, tryMode.value]');
if (mw < 0) fail('找不到 tryCharId/tryMode watcher');
const mwSeg = html.slice(mw, mw + 900);
if (!/v\[1\] === "img2img" \|\| v\[1\] === "swap"/.test(mwSeg)) {
  fail('换画法 watcher 未在 swap 模式加载 tryRefs（「身份」弹窗必空）');
}

// 7) 源数组有数据 → 传给 picker 的 items 必须非空（真跑 computed，不是看字符串）
const cs = html.indexOf('function _tryRefPickerItem(r, i) {');
const ce = html.indexOf('\n      function openTryRefPicker()', cs);
if (cs < 0 || ce < 0) fail('找不到 _tryRefPickerItem / picker computed 区块');
const compSrc = html.slice(cs, ce);
const fakeVue = { computed: fn => ({ get value() { return fn(); } }) };
const fakeGlobal = [{ path: 'global_refs/g1.webp', thumb: 'data:g', note: '画布',
                      exists: true, width: 2160, height: 3156, size: 12345 }];
const fakeRefs = [{ path: 'refs/r1.png', thumb: 'data:r', note: '主参考',
                    exists: true, width: 700, height: 1200, source: '卡池' }];
let built;
try {
  built = new Function('Vue', 'globalRefs', 'tryRefs', 'grName', 'grSize',
    compSrc + '\nreturn { a: tryRefPickerItems, b: trySwapGlobalPickerItems };')(
    fakeVue, { value: fakeGlobal }, { value: fakeRefs },
    r => String((r && r.path || '').split('/').pop()),
    n => String(n || 0) + ' B');
} catch (e) { fail('picker computed 求值抛错: ' + e.message); }
if (!built.a.value.length) fail('tryRefs 有数据但 tryRefPickerItems 为空');
if (!built.b.value.length) fail('globalRefs 有数据但 trySwapGlobalPickerItems 为空');
if (built.b.value[0].path !== 'global_refs/g1.webp') fail('picker item 丢 path');

// 8) v0.60.0：选中后展示缩略图（WPickThumb），不再只显示文件名
if (html.indexOf('var WPickThumb = {') < 0) fail('WPickThumb 未定义');
if (html.indexOf('app.component("w-pick-thumb", WPickThumb)') < 0) fail('WPickThumb 未全局注册');
if (html.indexOf('.pick-thumb-img') < 0) fail('缩略图样式缺失');
const ts = html.indexOf('var WPickThumb = {');
const te = html.indexOf('\n  };', ts);
const thumbSrc = html.slice(ts, te + 5);
let ThumbComp;
try {
  ThumbComp = (new Function('h', thumbSrc + '\nreturn WPickThumb;'))(h);
} catch (e) { fail('WPickThumb eval 抛错: ' + e.message); }
const emitted = [];
const thumbInst = { src: 'data:img', path: 'refs/g.webp', label: '① 画布：g.webp',
                    sub: '图1', index: 0, active: true,
                    $emit: function (ev, a, b) { emitted.push([ev, a, b]); } };
let thumbVnode;
try { thumbVnode = ThumbComp.render.call(thumbInst); } catch (e) {
  fail('WPickThumb render 抛错: ' + e.message);
}
// v0.60.3：根元素必须是 div（不再是整块 button），才能把「看大图」和「进弹窗」拆成两个区域
if (!thumbVnode || thumbVnode.tag !== 'div') fail('WPickThumb 根元素不是 div（点击区域无法拆分）');
const thumbKids = thumbVnode.children || [];
const imgNode = thumbKids.find(k => k && k.props && String(k.props.class).indexOf('pick-thumb-img') >= 0);
if (!imgNode) fail('选中后没展示缩略图 img（仍只显示名字）');
// 点缩略图 emit preview(path,label)，点根节点 emit click——两条路径互不干扰
if (typeof imgNode.props.onClick !== 'function') fail('缩略图没绑看大图点击');
imgNode.props.onClick({ stopPropagation: function () {} });
if (emitted.length !== 1 || emitted[0][0] !== 'preview' || emitted[0][1] !== 'refs/g.webp') {
  fail('点缩略图没 emit preview(path)');
}
emitted.length = 0;
if (typeof thumbVnode.props.onClick !== 'function') fail('根节点没绑进弹窗点击');
thumbVnode.props.onClick();
if (emitted.length !== 1 || emitted[0][0] !== 'click') fail('点文字区没 emit click');
// 占位块（无 path）点缩略图区应放行到根节点 click，不该 emit preview
const noPath = Object.assign({}, thumbInst, { src: '', path: '', $emit: function (ev) { emitted.push([ev]); } });
const noPathVnode = ThumbComp.render.call(noPath);
const phNode = (noPathVnode.children || []).find(k => k && k.props && String(k.props.class).indexOf('pick-thumb-ph') >= 0);
emitted.length = 0;
if (phNode && typeof phNode.props.onClick === 'function') phNode.props.onClick({ stopPropagation: function () {} });
if (emitted.some(e => e[0] === 'preview')) fail('占位块（无 path）不应 emit preview');
// 试画台 swap / 角色详情 swap / img2img 已选条三处都必须用缩略图
if ((html.match(/<w-pick-thumb /g) || []).length < 4) {
  fail('缩略图按钮挂载点不足（试画台 swap + 角色详情 swap 共 4 处）');
}
if (html.indexOf('class="picker-strip-thumb"') < 0) fail('img2img 已选条没加缩略图');
// v0.60.3：选中即存缩略图——4 个确认回调都要写对应的 thumb ref，挂载点优先用它
for (const [cb, ref] of [
  ['onTrySwapGlobalConfirm', 'trySwapGlobalThumb'],
  ['onTrySwapCharConfirm', 'trySwapCharThumb'],
  ['onGalSwapGlobalConfirm', 'galSwapGlobalThumb'],
  ['onGalSwapCharConfirm', 'galSwapCharThumb']
]) {
  const s = html.indexOf('function ' + cb + '(vals) {');
  if (s < 0) fail('找不到 ' + cb);
  const seg = html.slice(s, s + 420);
  if (seg.indexOf(ref + '.value =') < 0) fail(cb + ' 没存 ' + ref + '（选完仍会丢缩略图）');
}
if (!/galSwapChar\.value = null; galSwapCharThumb\.value = ""/.test(html)) {
  fail('换角色没清 galSwapChar 缩略图（会残留上一位角色的身份图）');
}

// 9) v0.60.0：角色详情 swap 也用图片弹窗，老文本下拉必须撤掉
const galSeg = html.split('v-if="galMode === \'swap\'" class="swap-panel"', 2)[1] || '';
if (!galSeg || galSeg.indexOf('</div>') < 0) fail('找不到角色详情 swap 面板');
if (galSeg.indexOf('<n-select v-model:value="galSwapGlobal"') >= 0
    || galSeg.indexOf('<n-select v-model:value="galSwapChar"') >= 0) {
  fail('角色详情 swap 仍是老文本下拉');
}
for (const tag of ['galSwapGlobalPickerOpen', 'galSwapCharPickerOpen']) {
  if (html.indexOf('v-model:show="' + tag + '"') < 0) fail('角色详情未挂 ' + tag);
}
if (!/var galSwapCharPickerItems = Vue\.computed/.test(html)) fail('角色详情身份图候选未定义');
for (const key of ['galSwapGlobalPickerOpen', 'galSwapCharPickerOpen',
                   'galSwapCharPickerItems', 'openGalSwapGlobalPicker',
                   'openGalSwapCharPicker', 'onGalSwapGlobalConfirm',
                   'onGalSwapCharConfirm', 'pickThumbOf']) {
  if (!new RegExp('\\b' + key + ': ' + key + '[,}]').test(html)) {
    fail('setup 未暴露 ' + key + '（模板取不到）');
  }
}
// 角色详情身份图候选真跑：genSources 有 ref 数据时 picker items 非空
const gs = html.indexOf('var galSwapCharPickerItems = Vue.computed(function () {');
if (gs < 0) fail('找不到 galSwapCharPickerItems 定义');
const ge = html.indexOf('\n      function openGalSwapGlobalPicker()', gs);
let galBuilt;
try {
  galBuilt = new Function('Vue', 'genSources',
    html.slice(gs, ge) + '\nreturn galSwapCharPickerItems;')(
    fakeVue, { value: [
      { kind: 'ref', path: 'refs/r1.png', thumb: 'data:r' },
      { kind: 'portrait', path: 'portraits/c1/p1.png', thumb: 'data:p' }
    ] });
} catch (e) { fail('galSwapCharPickerItems 求值抛错: ' + e.message); }
if (galBuilt.value.length !== 1 || galBuilt.value[0].path !== 'refs/r1.png') {
  fail('角色详情身份图候选口径错（应只含 ref，不含立绘）');
}

console.log('test_image_picker: ALL OK');
