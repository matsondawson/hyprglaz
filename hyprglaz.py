#!/usr/bin/env python3

import argparse
import gi
import json
import os
import re
import subprocess
import sys

gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gio, Gtk


def select_region():
    result = subprocess.run(['slurp'], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def parse_region(region):
    m = re.match(r'(\d+),(\d+)\s+(\d+)x(\d+)', region)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def active_workspace_id():
    raw = subprocess.check_output(['hyprctl', 'activeworkspace', '-j'], text=True)
    return json.loads(raw)['id']


def find_window(rx, ry, rw, rh, ws_id):
    raw = subprocess.check_output(['hyprctl', 'clients', '-j'], text=True)
    matches = []
    for c in json.loads(raw):
        if not c.get('mapped') or c.get('hidden'):
            continue
        if c.get('workspace', {}).get('id') != ws_id:
            continue
        cx, cy = c['at']
        cw, ch = c['size']
        if cx < rx + rw and cx + cw > rx and cy < ry + rh and cy + ch > ry:
            matches.append(c)
    if not matches:
        return None
    return min(matches, key=lambda c: (0 if c.get('floating') else 1,
                                       c.get('focusHistoryID', 9999)))


def re_escape(s):
    return re.sub(r'([.+*?^${}()\[\]|\\])', r'\\\1', s)


def gen_name(iclass, ititle):
    parts = [p for p in (iclass, ititle) if p]
    base = '-'.join(parts) if parts else 'window'
    base = re.sub(r'[^a-z0-9]+', '-', base.lower()).strip('-')
    return f'glaz-{base}'


def build_rule(name, class_, iclass, title, ititle, size, prop):
    lines = ['windowrule {', f'  name = {name}']
    if class_:
        lines.append(f'  match:class = {class_}')
    if iclass:
        lines.append(f'  match:initial_class = {iclass}')
    if title:
        lines.append(f'  match:title = {title}')
    if ititle:
        lines.append(f'  match:initial_title = {ititle}')
    lines.append('')
    if size:
        lines.append(f'  size = {size}')
    for p in prop.splitlines():
        if p.strip():
            lines.append(f'  {p.strip()}')
    lines.append('}')
    return '\n'.join(lines)


DEFAULT_CONF = '~/.config/hypr/conf/windowrules/custom.conf'


def save_rule(rule_text, name, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if not os.path.exists(path):
        with open(path, 'w') as f:
            f.write(rule_text + '\n')
        return 'appended'

    with open(path, 'r') as f:
        lines = f.readlines()

    result = []
    replaced = False
    i = 0

    while i < len(lines):
        line = lines[i]
        if re.match(r'\s*windowrule\s*\{', line):
            block = [line]
            depth = line.count('{') - line.count('}')
            i += 1
            while i < len(lines) and depth > 0:
                block.append(lines[i])
                depth += lines[i].count('{') - lines[i].count('}')
                i += 1
            if re.search(rf'^\s*name\s*=\s*{re.escape(name)}\s*$',
                         ''.join(block), re.MULTILINE):
                result.append(rule_text + '\n')
                replaced = True
            else:
                result.extend(block)
        else:
            result.append(line)
            i += 1

    if not replaced:
        if result and not result[-1].endswith('\n'):
            result.append('\n')
        result.append('\n' + rule_text + '\n')

    with open(path, 'w') as f:
        f.writelines(result)

    return 'replaced' if replaced else 'appended'


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
            'prop':   'float = on' if win_info.get('floating') else '',
        }

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.set_child(root)

        grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        root.append(grid)

        self.entries = {}
        for row, (key, label) in enumerate(FIELDS):
            lbl = Gtk.Label(label=label)
            lbl.set_halign(Gtk.Align.END)
            lbl.set_size_request(110, -1)
            entry = Gtk.Entry()
            entry.set_text(defaults[key])
            entry.set_hexpand(True)
            entry.connect('changed', lambda _e: self._refresh())
            grid.attach(lbl,   0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
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

        prop_row = len(FIELDS)
        grid.attach(prop_lbl,    0, prop_row, 1, 1)
        grid.attach(prop_scroll, 1, prop_row, 1, 1)

        sep = Gtk.Separator()
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        root.append(sep)

        lbl = Gtk.Label(label='Generated Rule')
        lbl.set_halign(Gtk.Align.START)
        root.append(lbl)

        self._buf = Gtk.TextBuffer()
        tv = Gtk.TextView(buffer=self._buf)
        tv.set_editable(False)
        tv.set_monospace(True)
        tv.set_left_margin(8)
        tv.set_top_margin(6)
        tv.set_bottom_margin(6)
        tv.set_wrap_mode(Gtk.WrapMode.NONE)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(140)
        scroll.set_vexpand(True)
        scroll.set_child(tv)
        root.append(scroll)

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

    def _values(self):
        v = {k: e.get_text() for k, e in self.entries.items()}
        buf = self._prop_buf
        v['prop'] = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        return v

    def _current_rule(self):
        v = self._values()
        return build_rule(v['name'], v['class'], v['iclass'],
                          v['title'], v['ititle'], v['size'], v['prop'])

    def _refresh(self):
        self._buf.set_text(self._current_rule(), -1)

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
        description='Select a window region and generate a Hyprland windowrule.',
    )
    parser.add_argument(
        '-o', '--output',
        metavar='FILE',
        default=DEFAULT_CONF,
        help=f'Config file to save rules into (default: {DEFAULT_CONF})',
    )
    args = parser.parse_args()

    region = select_region()
    if not region:
        sys.exit(0)

    coords = parse_region(region)
    if not coords:
        error_app('Could not parse slurp output.')
        sys.exit(1)

    try:
        ws_id = active_workspace_id()
        win_info = find_window(*coords, ws_id)
    except Exception as e:
        error_app(str(e))
        sys.exit(1)

    if not win_info:
        error_app('No window found in the selected region.')
        sys.exit(1)

    sys.exit(HyprGlazApp(win_info, os.path.expanduser(args.output)).run([]))


if __name__ == '__main__':
    main()
