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
console.log('test_image_picker: ALL OK');
