#!/usr/bin/env python3

import argparse
import cairo
import gi
import json
import os
import re
import subprocess
import sys
import tempfile

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, GLib, Gio, Gtk


def active_workspace_id():
    raw = subprocess.check_output(['hyprctl', 'activeworkspace', '-j'], text=True)
    return json.loads(raw)['id']


def _priority(c):
    return (0 if c.get('floating') else 1, c.get('focusHistoryID', 9999))


def _top_window_at(clients, ws_id, x, y):
    matches = [
        c for c in clients
        if c.get('mapped') and not c.get('hidden')
        and c.get('workspace', {}).get('id') == ws_id
        and c['at'][0] <= x < c['at'][0] + c['size'][0]
        and c['at'][1] <= y < c['at'][1] + c['size'][1]
    ]
    return min(matches, key=_priority) if matches else None


def _take_screenshot():
    fd, path = tempfile.mkstemp(suffix='.png', prefix='hyprglaz-')
    os.close(fd)
    r = subprocess.run(['grim', path], capture_output=True)
    if r.returncode != 0:
        os.unlink(path)
        return None
    return path


class _PickerApp(Gtk.Application):

    def __init__(self, clients, ws_id, screenshot_path):
        super().__init__(application_id='land.hypr.glaz.picker',
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._clients = clients
        self._ws_id = ws_id
        self._screenshot_path = screenshot_path
        self._screenshot_surface = None
        self.result = None
        self._hovered = None
        self.connect('activate', self._on_activate)

    def _on_activate(self, _app):
        if self._screenshot_path:
            self._screenshot_surface = cairo.ImageSurface.create_from_png(
                self._screenshot_path)

        win = Gtk.ApplicationWindow(application=self)
        win.set_decorated(False)
        win.set_title('hyprglaz-picker')
        win.fullscreen()

        area = Gtk.DrawingArea()
        area.set_draw_func(self._draw)
        area.set_hexpand(True)
        area.set_vexpand(True)
        win.set_child(area)
        self._area = area

        mc = Gtk.EventControllerMotion()
        mc.connect('motion', self._on_motion)
        win.add_controller(mc)

        gc = Gtk.GestureClick()
        gc.connect('pressed', self._on_click)
        win.add_controller(gc)

        kc = Gtk.EventControllerKey()
        kc.connect('key-pressed', self._on_key)
        win.add_controller(kc)

        win.present()
        self._win = win

    def _on_motion(self, _ctrl, x, y):
        h = _top_window_at(self._clients, self._ws_id, x, y)
        if h is not self._hovered:
            self._hovered = h
            self._area.queue_draw()

    def _draw(self, _area, cr, _w, _h):
        if self._screenshot_surface:
            cr.set_source_surface(self._screenshot_surface, 0, 0)
            cr.paint()
        cr.set_source_rgba(0, 0, 0, 0.4)
        cr.paint()
        if self._hovered:
            x, y = self._hovered['at']
            w, h = self._hovered['size']
            cr.set_source_rgba(0.15, 0.55, 1.0, 0.25)
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(0.15, 0.55, 1.0, 0.9)
            cr.set_line_width(3)
            cr.rectangle(x, y, w, h)
            cr.stroke()

    def _on_click(self, _g, _n, _x, _y):
        self.result = self._hovered
        self._win.close()
        self.quit()

    def _on_key(self, _c, keyval, _kc, _state):
        if keyval == Gdk.KEY_Escape:
            self._win.close()
            self.quit()
        return False


def pick_window(ws_id):
    screenshot_path = _take_screenshot()
    try:
        raw = subprocess.check_output(['hyprctl', 'clients', '-j'], text=True)
        clients = json.loads(raw)
        app = _PickerApp(clients, ws_id, screenshot_path)
        app.run([])
        return app.result
    finally:
        if screenshot_path and os.path.exists(screenshot_path):
            os.unlink(screenshot_path)


def re_escape(s):
    return re.sub(r'([.+*?^${}()\[\]|\\])', r'\\\1', s)


def lua_str(s):
    if '\\' in s and ']]' not in s and not s.endswith(']'):
        return f'[[{s}]]'
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def gen_name(iclass, ititle):
    parts = [p for p in (iclass, ititle) if p]
    base = '-'.join(parts) if parts else 'window'
    base = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')
    return f'glaz-{base}'


def build_rule(name, class_, iclass, title, ititle, size, prop):
    lines = ['hl.window_rule({', f'    name  = {lua_str(name)},']

    match = [f'        {key} = {lua_str(val)},' for key, val in (
        ('class', class_), ('initial_class', iclass),
        ('title', title), ('initial_title', ititle)) if val]
    if match:
        lines.append('    match = {')
        lines.extend(match)
        lines.append('    },')

    props = [f'    size  = {lua_str(size)},'] if size else []
    for p in prop.splitlines():
        s = p.strip()
        if s.startswith('--'):
            props.append(f'    {s}')
        elif s:
            props.append(f'    {s.rstrip(",")},')

    if props:
        lines.append('')
        lines.extend(props)
    lines.append('})')
    return '\n'.join(lines)


DEFAULT_CONF = '~/.config/hypr/conf/windowrules/custom.lua'

_RULE_START_RE = re.compile(r'^[^\S\n]*hl\.window_rule\s*\(\s*\{', re.MULTILINE)

_NAME_RE = re.compile(
    r'^\s*name\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|\[\[(.*?)\]\])\s*,?\s*$',
    re.MULTILINE)


def _blank_literals(text):
    """Blank out Lua strings and comments (keeping newlines) so braces can be counted."""
    out = list(text)
    i, n = 0, len(text)

    def blank(start, end):
        for k in range(start, min(end, n)):
            if out[k] != '\n':
                out[k] = ' '

    def close_at(start, token):
        end = text.find(token, start)
        return n if end < 0 else end + len(token)

    while i < n:
        if text.startswith('--[[', i):
            end = close_at(i + 4, ']]')
        elif text.startswith('--', i):
            end = text.find('\n', i)
            end = n if end < 0 else end
        elif text.startswith('[[', i):
            end = close_at(i + 2, ']]')
        elif text[i] in '"\'':
            j = i + 1
            while j < n and text[j] != text[i] and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            end = min(j + 1, n)
        else:
            i += 1
            continue
        blank(i, end)
        i = end

    return ''.join(out)


def _rule_blocks(text):
    """Yield (start, end) character spans of each hl.window_rule({ ... }) call."""
    blank = _blank_literals(text)
    for m in _RULE_START_RE.finditer(blank):
        depth = 0
        for i in range(m.start(), len(blank)):
            if blank[i] == '{':
                depth += 1
            elif blank[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    while end < len(blank) and blank[end] in ' \t':
                        end += 1
                    if end < len(blank) and blank[end] == ')':
                        end += 1
                    yield m.start(), end
                    break


def _block_name(block):
    m = _NAME_RE.search(block)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def _read_config(path):
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read()
    except OSError:
        return ''


def save_rule(rule_text, name, path):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = _read_config(path)

    for start, end in _rule_blocks(content):
        if _block_name(content[start:end]) == name:
            content = content[:start] + rule_text + content[end:]
            action = 'replaced'
            break
    else:
        sep = '' if not content.strip() else (
            '\n' if content.endswith('\n') else '\n\n')
        content = content + sep + rule_text + '\n'
        action = 'appended'

    with open(path, 'w') as f:
        f.write(content)

    return action


_PROP_LINE_RE = re.compile(r'^\s*[A-Za-z_]\w*\s*=\s*\S')


def _load_existing_names(path):
    content = _read_config(path)
    names = (_block_name(content[s:e]) for s, e in _rule_blocks(content))
    return [n for n in names if n]


def _find_existing_rule(name, path):
    content = _read_config(path)
    for start, end in _rule_blocks(content):
        block = content[start:end]
        if _block_name(block) == name:
            return block.rstrip()
    return None

FIELDS = [
    ('name',   'Name'),
    ('class',  'Class'),
    ('iclass', 'Initial Class'),
    ('title',  'Title'),
    ('ititle', 'Initial Title'),
    ('size',   'Size (W H)'),
]


class HyprGlazWindow(Gtk.ApplicationWindow):

    def __init__(self, app, win_info, output_path):
        super().__init__(application=app, title='HyprGlaz')
        self.set_default_size(680, -1)
        self.set_resizable(True)
        self._output_path = output_path

        sw, sh = win_info['size']
        class_  = win_info.get('class', '')
        iclass  = win_info.get('initialClass', '')
        title   = win_info.get('title', '')
        ititle  = win_info.get('initialTitle', '')

        defaults = {
            'name':   gen_name(iclass, ititle),
            'class':  re_escape(class_),
            'iclass': re_escape(iclass),
            'title':  re_escape(title),
            'ititle': re_escape(ititle),
            'size':   f'{sw} {sh}',
            'prop':   'float = true' if win_info.get('floating') else '',
        }

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        root.append(grid)

        existing_names = _load_existing_names(output_path)

        self.entries = {}
        name_row = next(i for i, (k, _) in enumerate(FIELDS) if k == 'name')
        for row, (key, label) in enumerate(FIELDS):
            # shift rows after name down by 1 to reserve space for suggestion bar
            grid_row = row if row <= name_row else row + 1
            lbl = Gtk.Label(label=label)
            lbl.set_halign(Gtk.Align.END)
            lbl.set_size_request(110, -1)
            if key == 'name':
                entry = self._make_name_entry(defaults[key], existing_names, grid, name_row)
            else:
                entry = Gtk.Entry()
                entry.set_text(defaults[key])
                entry.set_hexpand(True)
                entry.connect('changed', lambda _e: self._refresh())
            grid.attach(lbl,   0, grid_row, 1, 1)
            grid.attach(entry, 1, grid_row, 1, 1)
            self.entries[key] = entry

        prop_lbl = Gtk.Label(label='Extra Properties')
        prop_lbl.set_halign(Gtk.Align.END)
        prop_lbl.set_valign(Gtk.Align.START)
        prop_lbl.set_size_request(110, -1)

        self._prop_buf = Gtk.TextBuffer()
        self._prop_buf.set_text(defaults['prop'])
        self._prop_buf.connect('changed', lambda _b: self._refresh())
        prop_tv = Gtk.TextView(buffer=self._prop_buf)
        prop_tv.set_monospace(True)
        prop_tv.set_left_margin(8)
        prop_tv.set_top_margin(6)
        prop_tv.set_bottom_margin(6)
        prop_tv.set_wrap_mode(Gtk.WrapMode.NONE)
        prop_tv.set_hexpand(True)
        prop_scroll = Gtk.ScrolledWindow()
        prop_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        prop_scroll.set_min_content_height(80)
        prop_scroll.set_child(prop_tv)

        prop_row = len(FIELDS) + 1
        grid.attach(prop_lbl,    0, prop_row, 1, 1)
        grid.attach(prop_scroll, 1, prop_row, 1, 1)

        self._prop_warn = Gtk.Label()
        self._prop_warn.set_halign(Gtk.Align.START)
        self._prop_warn.add_css_class('error')
        self._prop_warn.set_visible(False)
        grid.attach(self._prop_warn, 1, prop_row + 1, 1, 1)

        sep = Gtk.Separator()
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        root.append(sep)

        rule_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        rule_row.set_vexpand(True)
        root.append(rule_row)

        def _make_rule_tv(buf):
            tv = Gtk.TextView(buffer=buf)
            tv.set_editable(False)
            tv.set_monospace(True)
            tv.set_left_margin(8)
            tv.set_top_margin(6)
            tv.set_bottom_margin(6)
            tv.set_wrap_mode(Gtk.WrapMode.NONE)
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            sw.set_min_content_height(140)
            sw.set_vexpand(True)
            sw.set_hexpand(True)
            sw.set_child(tv)
            return sw

        new_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        new_col.set_hexpand(True)
        new_lbl = Gtk.Label(label='Generated Rule')
        new_lbl.set_halign(Gtk.Align.START)
        new_col.append(new_lbl)
        self._buf = Gtk.TextBuffer()
        new_col.append(_make_rule_tv(self._buf))
        rule_row.append(new_col)

        self._existing_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._existing_col.set_hexpand(True)
        self._existing_col.set_visible(False)
        existing_lbl = Gtk.Label(label='Existing Rule')
        existing_lbl.set_halign(Gtk.Align.START)
        self._existing_col.append(existing_lbl)
        self._existing_buf = Gtk.TextBuffer()
        self._existing_col.append(_make_rule_tv(self._existing_buf))
        rule_row.append(self._existing_col)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(8)
        root.append(btn_row)

        close_btn = Gtk.Button(label='Close')
        close_btn.connect('clicked', lambda _: self.close())
        btn_row.append(close_btn)

        self._copy_btn = Gtk.Button(label='Copy Rule')
        self._copy_btn.add_css_class('suggested-action')
        self._copy_btn.connect('clicked', self._on_copy)
        btn_row.append(self._copy_btn)

        self._save_btn = Gtk.Button(label='Save to Config')
        self._save_btn.add_css_class('suggested-action')
        self._save_btn.connect('clicked', self._on_save)
        btn_row.append(self._save_btn)

        self._refresh()

    def _make_name_entry(self, default_text, existing_names, grid, name_row):
        entry = Gtk.Entry()
        entry.set_text(default_text)
        entry.set_hexpand(True)

        if not existing_names:
            entry.connect('changed', lambda _e: self._refresh())
            return entry

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_scroll = Gtk.ScrolledWindow()
        btn_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        btn_scroll.set_child(btn_box)
        btn_scroll.set_visible(False)
        grid.attach(btn_scroll, 1, name_row + 1, 1, 1)

        def on_changed(e):
            self._refresh()
            text = e.get_text().lower()
            while child := btn_box.get_first_child():
                btn_box.remove(child)
            matches = [n for n in existing_names if text in n.lower()] if text else []
            for name in matches:
                btn = Gtk.Button(label=name)
                btn.add_css_class('flat')
                def on_clicked(_b, n=name):
                    entry.set_text(n)
                    entry.grab_focus()
                btn.connect('clicked', on_clicked)
                btn_box.append(btn)
            btn_scroll.set_visible(bool(matches))

        entry.connect('changed', on_changed)
        return entry

    def _values(self):
        v = {k: e.get_text() for k, e in self.entries.items()}
        buf = self._prop_buf
        v['prop'] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        return v

    def _current_rule(self):
        v = self._values()
        return build_rule(v['name'], v['class'], v['iclass'],
                          v['title'], v['ititle'], v['size'], v['prop'])

    def _prop_errors(self, text):
        bad = []
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s and not s.startswith('--') and not _PROP_LINE_RE.match(line):
                bad.append(i)
        return bad

    def _refresh(self):
        self._buf.set_text(self._current_rule(), -1)
        existing = _find_existing_rule(self._values()['name'], self._output_path)
        if existing:
            self._existing_buf.set_text(existing, -1)
            self._existing_col.set_visible(True)
        else:
            self._existing_col.set_visible(False)
        v = self._values()
        errors = self._prop_errors(v['prop'])
        if errors:
            self._prop_warn.set_label(f'Invalid line(s): {", ".join(map(str, errors))} — expected: key = value')
            self._prop_warn.set_visible(True)
        else:
            self._prop_warn.set_visible(False)
        sensitive = not errors
        self._copy_btn.set_sensitive(sensitive)
        self._save_btn.set_sensitive(sensitive)

    def _on_copy(self, btn):
        subprocess.run(['wl-copy', '--', self._current_rule()])
        btn.set_label('Copied!')
        GLib.timeout_add(1500, self._reset_copy_label)

    def _reset_copy_label(self):
        self._copy_btn.set_label('Copy Rule')
        return False

    def _on_save(self, btn):
        v = self._values()
        action = save_rule(self._current_rule(), v['name'], self._output_path)
        btn.set_label(f'Saved ({action})')
        GLib.timeout_add(2000, self._reset_save_label)

    def _reset_save_label(self):
        self._save_btn.set_label('Save to Config')
        return False


class HyprGlazApp(Gtk.Application):
    def __init__(self, win_info, output_path):
        super().__init__(application_id='land.hypr.glaz',
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._win_info = win_info
        self._output_path = output_path
        self.connect('activate', self._on_activate)

    def _on_activate(self, app):
        HyprGlazWindow(app, self._win_info, self._output_path).present()


def error_app(msg):
    app = Gtk.Application(application_id='land.hypr.glaz.error',
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    def on_activate(a):
        win = Gtk.ApplicationWindow(application=a, title='HyprGlaz')
        win.set_default_size(380, -1)
        lbl = Gtk.Label(label=msg)
        lbl.set_margin_top(20)
        lbl.set_margin_bottom(20)
        lbl.set_margin_start(16)
        lbl.set_margin_end(16)
        win.set_child(lbl)
        win.present()
    app.connect('activate', on_activate)
    app.run([])


def main():
    parser = argparse.ArgumentParser(
        prog='hyprglaz',
        description='Select a window and generate a Hyprland windowrule.',
    )
    parser.add_argument(
        '-o', '--output',
        metavar='FILE',
        default=DEFAULT_CONF,
        help=f'Config file to save rules into (default: {DEFAULT_CONF})',
    )
    args = parser.parse_args()

    try:
        ws_id = active_workspace_id()
        win_info = pick_window(ws_id)
    except Exception as e:
        error_app(str(e))
        sys.exit(1)

    if not win_info:
        sys.exit(0)

    sys.exit(HyprGlazApp(win_info, os.path.expanduser(args.output)).run([]))


if __name__ == '__main__':
    main()
