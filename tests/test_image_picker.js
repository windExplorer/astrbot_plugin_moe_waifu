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
if (!vnode || vnode.tag !== 'NModal') fail('render 未返回 NModal');
const kids = vnode.slots.default();
if (!Array.isArray(kids) || kids.length < 3) fail('弹窗正文槽缺失');
const grid = kids.find(k => k && k.tag === 'div' && Array.isArray(k.children));
if (!grid || grid.children.length !== 2) fail('图片网格未正确构建');

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
console.log('test_image_picker: ALL OK');
