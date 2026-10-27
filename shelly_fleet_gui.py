"""Shelly Fleet — generic, multi-broker MQTT switch and power monitor.
"""
from __future__ import annotations

import csv
import json
import os
import queue
import re
import ssl
import threading
import time
import uuid
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:
    raise SystemExit('Install requirements: python -m pip install -r requirements.txt') from exc
try:
    import keyring
except ImportError:
    keyring = None

APP_NAME = 'Shelly Fleet'
APP_VERSION = '1.0.0'
AUTHOR_NAME = 'Sajjad Baghaee'
AUTHOR_GITHUB = 'https://github.com/sajjadbaghaee/'
DOCS_URL = 'https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Mqtt/'
MAX_DEVICES = 100
MAX_CACHED_PAGES = 8
FRESH_SECONDS = 120
REFRESH_INTERVAL_MS = 45000
DEFAULT_PORTS = {'tls': '8883', 'plain': '1883'}
MODES = {'tls': 'TLS (verified)', 'plain': 'Plain MQTT (unencrypted)'}
PASSWORD_SERVICE = 'Shelly Fleet Desktop MQTT Credentials'
BASE_DIR = Path(__file__).resolve().parent
# Device profiles are private local data, never bundled with the application.
# SHELLY_FLEET_DATA_DIR can be used for an isolated empty demo profile.
DATA_DIR = Path(os.environ.get('SHELLY_FLEET_DATA_DIR') or (Path(os.environ.get('APPDATA') or Path.home()) / 'ShellyFleetDesktop'))
CONFIG_FILE = DATA_DIR / 'fleet_config.json'
THEME = {'bg':'#0D1423','surface':'#162238','surface2':'#1D2D46',
         'line':'#31435D','fg':'#EDF4FF','muted':'#B6C7DC',
         'accent':'#4B9BFF','green':'#22B78A','red':'#E75C6C',
         'amber':'#F0B959','graph':'#68B5FF','entry':'#111D2E'}
FONT = 'Segoe UI' if os.name == 'nt' else 'DejaVu Sans'


def stamp():
    return datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def validate_prefix(text):
    text = text.strip().strip('/')
    if not text or len(text) > 160 or '#' in text or '+' in text or '\x00' in text:
        raise ValueError('Enter a valid MQTT prefix; MQTT wildcards are not allowed.')
    if any(not re.fullmatch(r'[A-Za-z0-9_.:-]+', p) for p in text.split('/')):
        raise ValueError('Topic prefix supports letters, digits, _, -, ., :, and / separators.')
    return text


def valid_port(text):
    try:
        return 1 <= int(str(text).strip()) <= 65535
    except (TypeError, ValueError):
        return False


def ports_from_config(src):
    ports = DEFAULT_PORTS.copy()
    stored = src.get('ports_by_mode')
    if isinstance(stored, dict):
        for mode in MODES:
            if valid_port(stored.get(mode)):
                ports[mode] = str(int(stored[mode]))
    return ports


def check_profile(profile, *, validate_ca=True):
    if profile.mode not in MODES:
        raise ValueError('Unknown MQTT connection type.')
    host = profile.host.strip()
    if not host or '://' in host or '/' in host or ':' in host:
        raise ValueError('Enter a broker hostname or IPv4 address without a scheme or port.')
    if not valid_port(profile.ports.get(profile.mode)):
        raise ValueError('Broker port must be 1–65535.')
    if validate_ca and profile.mode == 'tls' and profile.ca.strip() and not Path(profile.ca.strip()).is_file():
        raise ValueError('The selected CA certificate file does not exist.')
    return host, int(profile.ports[profile.mode])


def make_label(parent, text, size=10, bold=False, muted=False, bg=None):
    return tk.Label(parent, text=text, bg=bg or parent.cget('bg'),
                    fg=THEME['muted'] if muted else THEME['fg'],
                    font=(FONT, size, 'bold' if bold else 'normal'), anchor='w')


def button(parent, title, action, tone='accent', pady=9):
    bg = {'accent':THEME['accent'],'green':THEME['green'],
          'red':THEME['red'],'quiet':THEME['surface2']}[tone]
    return tk.Button(parent, text=title, command=action, bg=bg, fg='#FFFFFF',
        activebackground=bg, activeforeground='#FFFFFF',
        disabledforeground='#91A0B8', relief='flat', borderwidth=0,
        cursor='hand2', padx=13, pady=pady, font=(FONT, 10, 'bold'))


def entry(parent, var, show=None):
    return tk.Entry(parent, textvariable=var, show=show, font=(FONT, 10),
        bg=THEME['entry'], fg=THEME['fg'], insertbackground=THEME['fg'],
        disabledbackground=THEME['surface2'], disabledforeground=THEME['muted'],
        relief='flat', highlightthickness=1, highlightbackground=THEME['line'],
        highlightcolor=THEME['accent'])


@dataclass
class BrokerProfile:
    bid: str
    name: str
    host: str = ''
    mode: str = 'tls'
    ports: dict = field(default_factory=lambda: DEFAULT_PORTS.copy())
    username: str = ''
    ca: str = ''
    remember: bool = False

    def serialize(self):
        return {'id': self.bid, 'name': self.name, 'host': self.host,
                'mode': self.mode, 'ports_by_mode': self.ports.copy(),
                'username': self.username, 'ca': self.ca,
                'remember_password': self.remember}


@dataclass
class Device:
    uid: str
    name: str
    prefix: str
    switch_id: int = 0
    broker_id: str = 'default'
    state: bool | None = None
    power: float | None = None
    voltage: float | None = None
    current: float | None = None
    energy: float | None = None
    last_seen: float = 0.0
    last_time: str = 'Never'
    history: deque = field(default_factory=lambda: deque(maxlen=240))
    mqtt_online: bool | None = None  # Last Will/online topic; never persisted

    @property
    def command_topic(self):
        return f'{self.prefix}/command/switch:{self.switch_id}'

    @property
    def status_topic(self):
        return f'{self.prefix}/status/switch:{self.switch_id}'

    @property
    def error_topic(self):
        return f'{self.prefix}/error/switch:{self.switch_id}'

    @property
    def online_topic(self):
        return f'{self.prefix}/online'

    def fresh(self):
        return self.last_seen > 0 and time.monotonic()-self.last_seen < FRESH_SECONDS

    def update(self, packet):
        if not isinstance(packet, dict) or ('id' in packet and packet['id'] != self.switch_id):
            return False
        if not {'output','apower','voltage','current','aenergy'}.intersection(packet):
            return False
        if isinstance(packet.get('output'), bool):
            self.state = packet['output']
        for key, dest in [('apower','power'),('voltage','voltage'),('current','current')]:
            value = packet.get(key)
            if isinstance(value, (float, int)) and not isinstance(value, bool):
                setattr(self, dest, float(value))
        energy = packet.get('aenergy')
        if isinstance(energy, dict):
            val = energy.get('total')
            if isinstance(val, (int,float)) and not isinstance(val,bool):
                self.energy = float(val)  # Shelly aenergy.total is Wh, not kWh
        self.last_seen = time.monotonic()
        self.last_time = stamp()
        if self.power is not None:
            self.history.append((self.last_time, self.power))
        return True

    def serialize(self):
        return {'uid':self.uid,'name':self.name,'prefix':self.prefix,
                'switch_id':self.switch_id,'broker_id':self.broker_id}


def new_device(name, prefix, switch_id=0, broker_id='default'):
    name = name.strip()
    if not 1 <= len(name) <= 72:
        raise ValueError('Device name must be 1–72 characters.')
    prefix = validate_prefix(prefix)
    if not isinstance(switch_id, int) or not 0 <= switch_id <= 15:
        raise ValueError('Switch ID must be between 0 and 15.')
    return Device(uuid.uuid4().hex, name, prefix, switch_id, broker_id)


def metric(value, unit, places=1):
    return '—' if value is None else f'{value:,.{places}f} {unit}'


class DeviceDialog(tk.Toplevel):
    def __init__(self, app, device=None):
        super().__init__(app)
        self.app, self.previous, self.result = app, device, None
        self.title('Edit device' if device else 'Add device')
        self.configure(bg=THEME['surface'])
        self.geometry('580x450')
        self.resizable(False,False)
        self.transient(app)
        self.grab_set()
        area = tk.Frame(self,bg=THEME['surface'],padx=23,pady=20)
        area.pack(fill='both',expand=True)
        self.name=tk.StringVar(value=device.name if device else f'Plug {len(app.devices)+1}')
        self.prefix=tk.StringVar(value=device.prefix if device else '')
        self.channel=tk.StringVar(value=str(device.switch_id if device else 0))
        self.broker=tk.StringVar()
        ids = list(app.brokers)
        self.options = {app.broker_label(x): x for x in ids}
        current_id = device.broker_id if device else 'default'
        self.broker.set(next(k for k,v in self.options.items() if v==current_id))
        for caption,var in [('Friendly device name',self.name),
                            ('Exact MQTT topic prefix',self.prefix),
                            ('Switch channel (Plug S Gen3 = 0)',self.channel)]:
            make_label(area,caption,muted=True).pack(anchor='w')
            entry(area,var).pack(fill='x',ipady=7,pady=(4,12))
        make_label(area,'MQTT broker profile',muted=True).pack(anchor='w')
        self.selector=ttk.Combobox(area,textvariable=self.broker,values=list(self.options),state='readonly')
        self.selector.pack(fill='x',ipady=4,pady=(5,7))
        make_label(area,'Default is used unless another profile is selected.',size=9,muted=True).pack(anchor='w')
        foot=tk.Frame(area,bg=THEME['surface'])
        foot.pack(fill='x',pady=(18,0))
        button(foot,'Cancel',self.destroy,'quiet').pack(side='right')
        button(foot,'Save device',self.save).pack(side='right',padx=(0,8))
        self.bind('<Escape>',lambda e:self.destroy())
        self.bind('<Return>',lambda e:self.save())

    def save(self):
        try:
            bid=self.options[self.broker.get()]
            d=new_device(self.name.get(),self.prefix.get(),int(self.channel.get()),bid)
            if self.previous:
                d.uid=self.previous.uid
            elif len(self.app.devices)>=MAX_DEVICES:
                raise ValueError('Maximum of 100 devices reached.')
            for other in self.app.devices.values():
                if other.uid!=d.uid and other.broker_id==bid and other.prefix==d.prefix and other.switch_id==d.switch_id:
                    raise ValueError('Another device uses the same broker, topic prefix and switch channel.')
        except (ValueError,KeyError) as exc:
            messagebox.showerror('Invalid device',str(exc),parent=self)
            return
        self.result=d
        self.destroy()


class BrokerEditor(tk.Toplevel):
    """Edit one named broker; ports are separately remembered for TLS and plain MQTT."""
    def __init__(self,app,bid=None):
        super().__init__(app)
        self.app=app
        self.edit_bid=bid
        p=app.brokers[bid] if bid else BrokerProfile(uuid.uuid4().hex,'')
        self.previous=p
        self.result=None
        self.title('MQTT broker profile')
        self.geometry('720x735')
        self.minsize(620,680)
        self.configure(bg=THEME['surface'])
        self.transient(app)
        self.grab_set()
        body=tk.Frame(self,bg=THEME['surface'],padx=24,pady=15)
        body.pack(fill='both',expand=True)
        make_label(body,'MQTT broker profile',18,True).pack(anchor='w',pady=(0,10))
        self.vars={k:tk.StringVar(value=str(v)) for k,v in {
            'name':p.name,'host':p.host,'mode':p.mode,
            'port':p.ports.get(p.mode,DEFAULT_PORTS[p.mode]),
            'username':p.username,'password':app.passwords.get(p.bid,''),'ca':p.ca}.items()}
        self.remember=tk.BooleanVar(value=p.remember)
        self.ports=p.ports.copy()
        self.name_input=self._input(body,'Profile name','name')
        if bid=='default':
            self.name_input.configure(state='disabled')
        make_label(body,'Connection type',muted=True).pack(anchor='w')
        self.mode_select=ttk.Combobox(body,state='readonly',values=list(MODES.values()))
        self.mode_select.set(MODES[p.mode])
        self.mode_select.pack(fill='x',ipady=4,pady=(4,9))
        self.mode_select.bind('<<ComboboxSelected>>',self.mode_changed)
        self.help_label=make_label(body,'',size=9,muted=True)
        self.help_label.pack(anchor='w',pady=(0,7))
        for title,key in [('Broker host (no scheme or port)','host'),('Port','port'),
                          ('Username (optional)','username'),('Password (not stored in JSON)','password')]:
            self._input(body,title,key)
        make_label(body,'CA file — TLS only (optional for public CA)',muted=True).pack(anchor='w')
        r=tk.Frame(body,bg=THEME['surface']); r.pack(fill='x',pady=(4,9))
        self.ca_input=entry(r,self.vars['ca']);self.ca_input.pack(side='left',fill='x',expand=True,ipady=6)
        self.browse_btn=button(r,'Browse',self.browse,'quiet',5)
        self.browse_btn.pack(side='left',padx=(8,0))
        cb=tk.Checkbutton(body,text='Store password in OS Credential Manager (optional)',
             variable=self.remember,bg=THEME['surface'],fg=THEME['fg'],
             selectcolor=THEME['entry'],activebackground=THEME['surface'],
             activeforeground=THEME['fg'],state='normal' if keyring else 'disabled')
        cb.pack(anchor='w',pady=(7,0))
        self.mode_changed()
        foot=tk.Frame(body,bg=THEME['surface'])
        foot.pack(fill='x',side='bottom',pady=(13,0))
        button(foot,'Cancel',self.destroy,'quiet').pack(side='right')
        button(foot,'Save settings',self.save).pack(side='right',padx=(0,8))
        self.bind('<Escape>',lambda e:self.destroy())

    def _input(self,parent,label,key):
        make_label(parent,label,muted=True).pack(anchor='w')
        inp=entry(parent,self.vars[key],show='•' if key=='password' else None)
        inp.pack(fill='x',ipady=6,pady=(4,9))
        return inp

    def mode_changed(self,event=None):
        old=self.vars['mode'].get()
        new=next((mode for mode,caption in MODES.items() if caption==self.mode_select.get()),'tls')
        if new!=old:
            self.ports[old]=self.vars['port'].get().strip()
            self.vars['mode'].set(new)
            self.vars['port'].set(self.ports.get(new,DEFAULT_PORTS[new]))
        secure=new=='tls'
        self.ca_input.configure(state='normal' if secure else 'disabled')
        self.browse_btn.configure(state='normal' if secure else 'disabled')
        self.help_label.configure(text=('Verified TLS: default port 8883; each mode keeps its own last saved port.'
                                  if secure else 'Unencrypted MQTT: use on LAN or through a VPN; default port 1883.'))

    def browse(self):
        path=filedialog.askopenfilename(parent=self,title='Choose trusted CA certificate',
                     filetypes=[('Certificates','*.crt *.pem *.cer'),('All files','*.*')])
        if path:self.vars['ca'].set(path)

    def save(self):
        self.ports[self.vars['mode'].get()]=self.vars['port'].get().strip()
        try:
            name=self.vars['name'].get().strip() if self.edit_bid!='default' else 'Default'
            if not name or len(name)>55:
                raise ValueError('Broker name must be 1–55 characters.')
            if any(x.bid!=self.previous.bid and x.name.casefold()==name.casefold() for x in self.app.brokers.values()):
                raise ValueError('Broker names must be unique.')
            if any(not valid_port(x) for x in self.ports.values()):
                raise ValueError('TLS and Plain ports must both be in range 1–65535.')
            p=BrokerProfile(self.previous.bid,name,self.vars['host'].get().strip(),
                 self.vars['mode'].get(),{k:str(int(v)) for k,v in self.ports.items()},
                 self.vars['username'].get().strip(),self.vars['ca'].get().strip(),
                 bool(self.remember.get()) and keyring is not None)
            if p.host:
                check_profile(p,validate_ca=False)
            if not p.host and p.bid!='default':
                raise ValueError('A named broker must have a hostname.')
        except ValueError as exc:
            messagebox.showerror('Invalid broker profile',str(exc),parent=self)
            return
        self.result=(p,self.vars['password'].get())
        self.destroy()


class BrokerManager(tk.Toplevel):
    def __init__(self,app):
        super().__init__(app)
        self.app=app
        self.title('MQTT broker profiles')
        self.geometry('815x510')
        self.minsize(660,440)
        self.configure(bg=THEME['surface'])
        self.transient(app)
        self.grab_set()
        b=tk.Frame(self,bg=THEME['surface'],padx=22,pady=18);b.pack(fill='both',expand=True)
        make_label(b,'MQTT broker profiles',19,True).pack(anchor='w')
        make_label(b,'Every device uses Default unless assigned another profile.',10,muted=True).pack(anchor='w',pady=(3,12))
        self.list=tk.Listbox(b,bg=THEME['entry'],fg=THEME['fg'],selectbackground=THEME['accent'],
                             selectforeground='white',font=(FONT,11),relief='flat',height=11)
        self.list.pack(fill='both',expand=True)
        make_label(b,'Changes reconnect only affected brokers when the fleet is online.',9,muted=True).pack(anchor='w',pady=(8,9))
        bar=tk.Frame(b,bg=THEME['surface']);bar.pack(fill='x')
        button(bar,'Add broker',self.add).pack(side='left')
        button(bar,'Edit selected',self.edit,'quiet').pack(side='left',padx=(8,0))
        button(bar,'Remove',self.remove,'red').pack(side='left',padx=(8,0))
        button(bar,'Import configuration…',self.import_existing,'quiet').pack(side='left',padx=(14,0))
        button(bar,'Close',self.destroy,'quiet').pack(side='right')
        self.list.bind('<Double-Button-1>',lambda e:self.edit())
        self.refresh()

    def refresh(self):
        self.ids=list(self.app.brokers)
        self.list.delete(0,'end')
        for bid in self.ids:
            p=self.app.brokers[bid]
            state='CONNECTED' if self.app.is_connected(bid) else 'DISCONNECTED'
            target=f'{p.host}:{p.ports[p.mode]}' if p.host else '(not configured)'
            self.list.insert('end',f'{p.name}     |     {target}     |     {state}')

    def selected(self):
        selected=self.list.curselection()
        return self.ids[selected[0]] if selected else None

    def add(self):
        dlg=BrokerEditor(self.app)
        self.wait_window(dlg)
        if dlg.result:
            p,pwd=dlg.result
            self.app.apply_broker(p,pwd)
            self.refresh()

    def edit(self):
        bid=self.selected()
        if bid is None:
            messagebox.showinfo('Select a broker','Choose a broker profile first.',parent=self)
            return
        dlg=BrokerEditor(self.app,bid)
        self.wait_window(dlg)
        if dlg.result:
            p,pwd=dlg.result
            self.app.apply_broker(p,pwd)
            self.refresh()

    def import_existing(self):
        if self.app.import_existing_settings(parent=self):
            self.refresh()

    def remove(self):
        bid=self.selected()
        if not bid:return
        if bid=='default':
            messagebox.showwarning('Default broker','The Default broker cannot be removed.',parent=self)
            return
        if any(d.broker_id==bid for d in self.app.devices.values()):
            messagebox.showwarning('Broker in use','Reassign this broker’s devices before removing the profile.',parent=self)
            return
        if messagebox.askyesno('Remove broker','Remove the selected broker profile from this application?',parent=self):
            self.app.delete_broker(bid)
            self.refresh()

def load_help_content():
    """Load public, editable Help pages from resources/help_content.json.

    No broker credentials or personal device configuration are stored in this file.
    """
    path = BASE_DIR / 'resources' / 'help_content.json'
    try:
        with path.open('r', encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or not data:
            raise ValueError('Help content must contain named sections.')
        supported = {'heading', 'body', 'notice', 'code', 'status_table'}
        for section, items in data.items():
            if not isinstance(section, str) or not isinstance(items, list):
                raise ValueError('Invalid Help section.')
            for item in items:
                if (not isinstance(item, list) or len(item) != 2
                    or item[0] not in supported or not isinstance(item[1], str)):
                    raise ValueError(f'Invalid Help entry in {section}.')
        return data
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {'Help': [
            ('heading', 'HELP FILE NOT AVAILABLE'),
            ('body', f'Cannot load {path.name}: {exc}'),
            ('body', 'Restore the resources folder from the release package.')
        ]}


HELP_CONTENT = load_help_content()


class DevicePage(tk.Frame):
    def __init__(self,parent,app,d):
        super().__init__(parent,bg=THEME['bg'])
        self.app,self.device=app,d
        head=tk.Frame(self,bg=THEME['bg']);head.pack(fill='x',pady=(0,10))
        heading=tk.Frame(head,bg=THEME['bg']);heading.pack(side='left',fill='x',expand=True)
        self.title_lbl=make_label(heading,d.name,23,True);self.title_lbl.pack(anchor='w')
        self.topic_lbl=make_label(heading,d.prefix,10,muted=True);self.topic_lbl.pack(anchor='w',pady=(4,0))
        button(head,'Edit',lambda:app.edit_device(d.uid),'quiet').pack(side='right',padx=(7,0))
        button(head,'Remove',lambda:app.remove_device(d.uid),'quiet').pack(side='right')
        broker_card=tk.Frame(self,bg=THEME['surface'],padx=15,pady=12)
        broker_card.pack(fill='x',pady=(0,11))
        current=tk.Frame(broker_card,bg=THEME['surface']);current.pack(fill='x')
        make_label(current,'CURRENT MQTT BROKER FOR THIS PLUG',10,True,muted=True).pack(side='left')
        self.broker_status=make_label(current,'● DISCONNECTED',10,True)
        self.broker_status.pack(side='right')
        self.broker_current=make_label(broker_card,'Not configured',13,True)
        self.broker_current.pack(fill='x',pady=(7,2))
        self.broker_details=make_label(broker_card,'',9,muted=True)
        self.broker_details.pack(fill='x',pady=(0,12))
        chooser=tk.Frame(broker_card,bg=THEME['surface']);chooser.pack(fill='x')
        make_label(chooser,'CHANGE THIS PLUG TO:',9,True,muted=True).pack(side='left',padx=(0,9))
        self.broker_var=tk.StringVar()
        # Tk Menubutton provides reliable readable text on Windows dark themes.
        self.broker_menu=tk.Menubutton(chooser,textvariable=self.broker_var,indicatoron=True,
            bg=THEME['entry'],fg=THEME['fg'],activebackground=THEME['surface2'],
            activeforeground=THEME['fg'],relief='flat',highlightthickness=1,
            highlightbackground=THEME['line'],width=39,anchor='w',padx=9,pady=8,
            font=(FONT,10),cursor='hand2')
        self.broker_menu.pack(side='left',fill='x',expand=True,padx=(0,8))
        self.broker_choices=tk.Menu(self.broker_menu,tearoff=0,
            bg=THEME['surface2'],fg=THEME['fg'],activebackground=THEME['accent'],
            activeforeground='#FFFFFF')
        self.broker_menu.configure(menu=self.broker_choices)
        button(chooser,'Apply broker',self.apply_broker,'accent',6).pack(side='left')
        self.sync_broker()
        self.status=tk.Label(self,text='● WAITING FOR STATUS',bg=THEME['surface2'],
            fg=THEME['amber'],padx=13,pady=9,anchor='w',font=(FONT,10,'bold'))
        self.status.pack(fill='x',pady=(0,12))
        control=tk.Frame(self,bg=THEME['surface'],padx=16,pady=14);control.pack(fill='x',pady=(0,14))
        make_label(control,'SWITCH CONTROL',10,True,muted=True).pack(anchor='w',pady=(0,9))
        row=tk.Frame(control,bg=THEME['surface']);row.pack(fill='x')
        self.btn_on=button(row,'TURN ON',lambda:app.send_command(d.uid,'on'),'green')
        self.btn_off=button(row,'TURN OFF',lambda:app.send_command(d.uid,'off'),'red')
        self.btn_toggle=button(row,'TOGGLE',lambda:app.send_command(d.uid,'toggle'))
        self.btn_refresh=button(row,'REFRESH STATUS',lambda:app.send_command(d.uid,'status_update'),'quiet')
        for b in (self.btn_on,self.btn_off,self.btn_toggle,self.btn_refresh):
            b.pack(side='left',fill='x',expand=True,padx=(0,7))
        make_label(control,'Commands require fresh device status. No retained commands.',9,muted=True).pack(anchor='w',pady=(9,0))
        metrics=tk.Frame(self,bg=THEME['bg']);metrics.pack(fill='x',pady=(0,14))
        self.metrics={}
        for i,(title,unit) in enumerate([('POWER','W'),('VOLTAGE','V'),('CURRENT','A'),('ENERGY TOTAL','Wh')]):
            metrics.grid_columnconfigure(i,weight=1,uniform='metric')
            box=tk.Frame(metrics,bg=THEME['surface'],padx=11,pady=12)
            box.grid(row=0,column=i,sticky='nsew',padx=(0,8 if i<3 else 0))
            make_label(box,title,9,True,muted=True).pack(anchor='w')
            label=make_label(box,'—',19,True);label.pack(anchor='w',pady=(6,0))
            self.metrics[title]=(label,unit)
        chart=tk.Frame(self,bg=THEME['surface'],padx=15,pady=11);chart.pack(fill='both',expand=True,pady=(0,12))
        title=tk.Frame(chart,bg=THEME['surface']);title.pack(fill='x')
        make_label(title,'LIVE POWER HISTORY',11,True).pack(side='left')
        button(title,'Export CSV',lambda:app.export_history(d.uid),'quiet',5).pack(side='right')
        make_label(chart,'Session only  ·  most recent 240 reported samples',9,muted=True).pack(anchor='w')
        self.graph=tk.Canvas(chart,bg=THEME['surface'],height=140,highlightthickness=0)
        self.graph.pack(fill='both',expand=True,pady=(8,0))
        self.graph.bind('<Configure>',lambda e:self.draw_graph())
        self.last_lbl=make_label(self,'Last device update: Never',9,muted=True)
        self.last_lbl.pack(anchor='w')
        self.refresh()

    def sync_broker(self):
        self.options={self.app.broker_choice_label(bid):bid for bid in self.app.brokers}
        self.broker_choices.delete(0,'end')
        for label in self.options:
            self.broker_choices.add_radiobutton(label=label,variable=self.broker_var,value=label,
                selectcolor=THEME['entry'])
        self.broker_var.set(next((label for label,bid in self.options.items()
                                  if bid==self.device.broker_id),'Unknown broker'))

    def apply_broker(self):
        bid=self.options.get(self.broker_var.get())
        if bid:
            self.app.assign_broker(self.device.uid,bid)

    def refresh(self):
        d=self.device
        self.title_lbl.configure(text=d.name)
        self.topic_lbl.configure(text=f'{d.prefix}  •  switch:{d.switch_id}')
        connected=self.app.is_connected(d.broker_id)
        sub=d.uid in self.app.subscribed
        fresh=d.fresh()
        profile=self.app.brokers.get(d.broker_id)
        session_connected=self.app.is_connected(d.broker_id)
        if profile:
            dest=profile.host or 'Hostname not configured'
            port=profile.ports.get(profile.mode,'?')
            self.broker_current.configure(text=f'{profile.name}  ·  {dest}:{port}')
            self.broker_details.configure(text=f'Connection type: {MODES[profile.mode]}  |  This is the CURRENT saved assignment')
        else:
            self.broker_current.configure(text='Broker assignment not found')
            self.broker_details.configure(text='Choose another profile and click Apply broker')
        if session_connected:
            self.broker_status.configure(text='● BROKER CONNECTED',fg=THEME['green'])
        else:
            self.broker_status.configure(text='● BROKER DISCONNECTED',fg=THEME['red'])
        if not connected:
            t,color='● ASSIGNED BROKER DISCONNECTED — LAST KNOWN DATA',THEME['muted']
        elif d.uid in self.app.failed_subs:
            t,color='● SUBSCRIPTION DENIED — CHECK BROKER ACL',THEME['red']
        elif not sub:
            t,color='● SUBSCRIBING TO DEVICE TOPICS',THEME['amber']
        elif d.mqtt_online is False:
            t,color='● PLUG MQTT OFFLINE — LAST KNOWN RELAY STATE',THEME['red']
        elif not fresh:
            t,color='● NO FRESH STATUS FROM DEVICE',THEME['amber']
        elif d.state is True:
            t,color='● SWITCH ON',THEME['green']
        elif d.state is False:
            t,color='● SWITCH OFF',THEME['red']
        else:
            t,color='● STATUS RECEIVED',THEME['accent']
        self.status.configure(text=t,fg=color)
        for title,(label,unit) in self.metrics.items():
            value={'POWER':d.power,'VOLTAGE':d.voltage,'CURRENT':d.current,'ENERGY TOTAL':d.energy}[title]
            label.configure(text=metric(value,unit,3 if title=='CURRENT' else 1))
        active=connected and sub and fresh and d.state is not None and d.mqtt_online is not False
        for b in (self.btn_on,self.btn_off,self.btn_toggle):
            b.configure(state='normal' if active else 'disabled')
        self.btn_refresh.configure(state='normal' if connected and sub else 'disabled')
        self.last_lbl.configure(text=f'Last device update: {d.last_time}')
        if self.winfo_ismapped():
            self.draw_graph()

    def draw_graph(self):
        c=self.graph;c.delete('all')
        w,h=c.winfo_width(),c.winfo_height()
        if w<80 or h<55:return
        left,top,right,bottom=46,13,w-14,h-25
        for j in range(4):
            yy=top+(bottom-top)*j/3
            c.create_line(left,yy,right,yy,fill=THEME['line'],dash=(3,5))
        points=list(self.device.history)
        if not points:
            c.create_text(w/2,h/2,text='Waiting for live power data…',fill=THEME['muted'])
            return
        ymax=max(10.0,max(max(0,p) for _,p in points)*1.15)
        c.create_text(5,top,text=f'{ymax:.0f}W',anchor='nw',fill=THEME['muted'])
        c.create_text(10,bottom-8,text='0W',anchor='nw',fill=THEME['muted'])
        coords=[]
        for i,(_,power) in enumerate(points):
            x=left+(right-left)*i/max(1,len(points)-1)
            y=bottom-(bottom-top)*max(0,power)/ymax
            coords.extend([x,y])
        if len(points)>=2:
            c.create_line(*coords,fill=THEME['graph'],width=2)
        c.create_oval(coords[-2]-3,coords[-1]-3,coords[-2]+3,coords[-1]+3,fill=THEME['graph'],outline='')
        c.create_text(left,h-7,text=points[0][0][11:19],anchor='w',fill=THEME['muted'])
        c.create_text(right,h-7,text=points[-1][0][11:19],anchor='e',fill=THEME['muted'])

class ShellyFleetApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP_NAME} — Multi-Broker MQTT Remote Control')
        self.geometry('1290x810')
        self.minsize(1070,700)
        self.configure(bg=THEME['bg'])
        self.brokers: dict[str,BrokerProfile]={'default':BrokerProfile('default','Default')}
        self.passwords:dict[str,str]={}
        self.devices:dict[str,Device]={}
        self.sessions:dict[str,dict]={}
        self.subscribed:set[str]=set()
        self.failed_subs:set[str]=set()
        self.status_index:dict[tuple[str,str],str]={}
        self.error_index:dict[tuple[str,str],str]={}
        self.online_index:dict[tuple[str,str],set[str]]={}
        self.events=queue.Queue()
        self.want_connected=False
        self.closing=False
        self.approved_plain=set()
        self._after_tasks=[]
        self.pages:dict[str,DevicePage]={}
        self.selected_uid=None
        self.sidebar_buttons={}
        self.sidebar_indicators={}
        self.row_widgets={}
        self.log_lines=deque(maxlen=300)
        self.search_var=tk.StringVar()
        self._load_config()
        self._make_ui()
        self.search_var.trace_add('write',lambda *_:self.draw_sidebar())
        self.draw_sidebar()
        self.show_overview()
        self._after_tasks.append(self.after(150,self._poll_events))
        self._after_tasks.append(self.after(5000,self._maintenance))
        self._after_tasks.append(self.after(REFRESH_INTERVAL_MS,self._auto_refresh))
        self.protocol('WM_DELETE_WINDOW',self._close)
        self.log('Configure Default under Broker profiles; add devices and connect.')
        for bid,p in self.brokers.items():
            if p.remember and keyring is not None:
                threading.Thread(target=self._retrieve_password,args=(bid,),daemon=True).start()

    def _load_config(self, path=None):
        try:
            data=json.loads((path or CONFIG_FILE).read_text(encoding='utf-8'))
            if not isinstance(data,dict):data={}
        except (OSError,ValueError):
            data={}
        if isinstance(data.get('brokers'),list):
            for raw in data['brokers']:
                if not isinstance(raw,dict):continue
                try:
                    bid=str(raw['id']); name=str(raw['name'])
                    if not bid or not name:continue
                    mode=raw.get('mode','tls'); mode=mode if mode in MODES else 'tls'
                    p=BrokerProfile(bid,name,str(raw.get('host','')),mode,
                        ports_from_config(raw),str(raw.get('username','')),
                        str(raw.get('ca','')),bool(raw.get('remember_password',False)))
                    self.brokers[bid]=p
                except (KeyError,ValueError,TypeError):pass
            # The Default ID is always reserved, even if malformed data omits it.
            if 'default' not in self.brokers:
                self.brokers['default']=BrokerProfile('default','Default')
            self.brokers['default'].name='Default'
        items=data.get('devices',[])
        if isinstance(items,list):
            for raw in items[:MAX_DEVICES]:
                if not isinstance(raw,dict):continue
                try:
                    bid=str(raw.get('broker_id','default'))
                    if bid not in self.brokers:bid='default'
                    d=new_device(str(raw['name']),str(raw['prefix']),int(raw.get('switch_id',0)),bid)
                    uid=str(raw.get('uid') or d.uid)
                    if not re.fullmatch(r'[a-zA-Z0-9_-]{4,80}',uid):uid=d.uid
                    d.uid=uid
                    if any(x.uid==uid or (x.broker_id==bid and x.prefix==d.prefix and x.switch_id==d.switch_id) for x in self.devices.values()):continue
                    self.devices[uid]=d
                except (ValueError,TypeError,KeyError):pass
        self._rebuild_index()


    def import_existing_settings(self, parent=None):
        """Import a user-selected configuration file after explicit confirmation."""
        parent=parent or self
        if self.want_connected or self.sessions:
            messagebox.showwarning('Disconnect first',
                'Disconnect all MQTT brokers before importing a configuration.',parent=parent)
            return False
        candidate=filedialog.askopenfilename(parent=parent,
            title='Select a configuration file to import (private local data)',
            initialdir=str(Path.home()),
            initialfile='fleet_config.json',
            filetypes=[('Shelly Fleet configuration','*.json'),('JSON files','*.json')])
        if not candidate:return False
        source=Path(candidate)
        try:
            if source.resolve()==CONFIG_FILE.resolve():
                raise ValueError('That configuration is already active.')
            if source.stat().st_size>2_000_000:
                raise ValueError('Configuration exceeds the 2 MB import limit.')
            raw=json.loads(source.read_text(encoding='utf-8'))
            if not isinstance(raw,dict) or not isinstance(raw.get('devices',[]),list) or not (
                isinstance(raw.get('brokers'),list)):
                raise ValueError('This is not a recognized Shelly Fleet configuration.')
            device_count=len(raw.get('devices',[]))
            if device_count>MAX_DEVICES:
                raise ValueError('This file contains more than 100 devices.')
        except (OSError,ValueError,TypeError) as exc:
            messagebox.showerror('Import failed',str(exc),parent=parent)
            return False
        warning=(f'Import {device_count} device profiles from this local file?\n\n'
                 'The imported configuration may contain private MQTT hostnames, '
                 'usernames, device IDs, and local certificate paths.\n\n'
                 'This operation REPLACES your current broker and device profiles. '
                 'The source file will remain unchanged. No data is uploaded. '
                 'Passwords must be entered separately after import.\n\n'
                 'Continue?')
        if not messagebox.askyesno('Import private local settings',warning,parent=parent):
            return False
        for page in self.pages.values():page.destroy()
        self.pages.clear()
        self.brokers={'default':BrokerProfile('default','Default')}
        self.devices={}
        self.passwords={}
        self._load_config(source)
        # Imported credentials are intentionally never accessed or copied.
        for profile in self.brokers.values():
            profile.remember = False
        if not self.save_config():return False
        self.draw_sidebar()
        self.show_overview()
        self.refresh_views()
        self.log(f'Imported {len(self.devices)} private device profiles from a user-selected file.')
        messagebox.showinfo('Import complete',
            f'Imported {len(self.devices)} devices and {len(self.brokers)} broker profiles.\n'
            'Review broker settings before connecting.',parent=parent)
        return True

    def save_config(self):
        payload={'schema_version':1,'brokers':[p.serialize() for p in self.brokers.values()],
                 'devices':[d.serialize() for d in self.devices.values()]}
        try:
            DATA_DIR.mkdir(parents=True,exist_ok=True)
            tmp=CONFIG_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(payload,indent=2),encoding='utf-8')
            tmp.replace(CONFIG_FILE)
            return True
        except OSError as exc:
            messagebox.showerror('Cannot save configuration',str(exc),parent=self)
            return False

    def broker_label(self,bid):
        p=self.brokers[bid]
        return f'{p.name} (Default)' if bid=='default' else p.name

    def broker_choice_label(self,bid):
        p=self.brokers[bid]
        host=p.host or 'not configured'
        port=p.ports.get(p.mode,DEFAULT_PORTS[p.mode])
        transport='TLS' if p.mode=='tls' else 'Plain'
        return f'{p.name} — {host}:{port} ({transport})'

    def _rebuild_index(self):
        self.status_index={(d.broker_id,d.status_topic):d.uid for d in self.devices.values()}
        self.error_index={(d.broker_id,d.error_topic):d.uid for d in self.devices.values()}
        self.online_index={}
        for d in self.devices.values():
            self.online_index.setdefault((d.broker_id,d.online_topic),set()).add(d.uid)

    def is_connected(self,bid):
        sess=self.sessions.get(bid)
        return bool(sess and sess['ready'])

    def _make_ui(self):
        top=tk.Frame(self,bg=THEME['bg'],padx=22,pady=15);top.pack(fill='x')
        make_label(top,'SHELLY FLEET',21,True).pack(side='left')
        make_label(top,'  /  MULTI-BROKER MQTT CONTROL',9,muted=True).pack(side='left',pady=(8,0))
        self.connect_btn=button(top,'Connect all',self.toggle_connections)
        self.connect_btn.pack(side='right')
        button(top,'Broker profiles',lambda:BrokerManager(self),'quiet').pack(side='right',padx=(0,8))
        button(top,'About',self.show_about,'quiet').pack(side='right',padx=(0,8))
        button(top,'Help',self.show_help,'quiet').pack(side='right',padx=(0,8))
        self.broker_lbl=make_label(top,'● DISCONNECTED',10,True)
        self.broker_lbl.pack(side='right',padx=(0,13))
        body=tk.Frame(self,bg=THEME['bg']);body.pack(fill='both',expand=True,padx=17,pady=(0,12))
        side=tk.Frame(body,bg=THEME['surface'],width=260);side.pack(side='left',fill='y',padx=(0,14))
        side.pack_propagate(False)
        inner=tk.Frame(side,bg=THEME['surface'],padx=11,pady=13);inner.pack(fill='x')
        self.device_count=make_label(inner,'DEVICES 0/100',11,True);self.device_count.pack(anchor='w',pady=(0,9))
        entry(inner,self.search_var).pack(fill='x',ipady=7,pady=(0,10))
        button(inner,'+ Add plug',self.add_device).pack(fill='x')
        button(side,'Overview / All devices',self.show_overview,'quiet').pack(fill='x',padx=11,pady=(0,7))
        frame=tk.Frame(side,bg=THEME['surface']);frame.pack(fill='both',expand=True,padx=(5,1),pady=(0,10))
        self.side_canvas=tk.Canvas(frame,bg=THEME['surface'],highlightthickness=0)
        scrollbar=tk.Scrollbar(frame,command=self.side_canvas.yview)
        scrollbar.pack(side='right',fill='y');self.side_canvas.pack(side='left',fill='both',expand=True)
        self.side_canvas.configure(yscrollcommand=scrollbar.set)
        self.side_inner=tk.Frame(self.side_canvas,bg=THEME['surface'])
        window=self.side_canvas.create_window((0,0),window=self.side_inner,anchor='nw')
        self.side_inner.bind('<Configure>',lambda e:self.side_canvas.configure(scrollregion=self.side_canvas.bbox('all')))
        self.side_canvas.bind('<Configure>',lambda e:self.side_canvas.itemconfigure(window,width=e.width))
        self.side_canvas.bind('<MouseWheel>',lambda e:self.side_canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        self.content=tk.Frame(body,bg=THEME['bg']);self.content.pack(side='left',fill='both',expand=True)
        self.overview=tk.Frame(self.content,bg=THEME['bg'])
        self._make_overview()
        foot=tk.Frame(self,bg=THEME['bg'],padx=21,pady=6);foot.pack(fill='x')
        make_label(foot,'Multiple broker sessions  •  Up to 100 devices  •  Energy in Wh',9,muted=True).pack(side='left')
        devlink=tk.Label(foot,text='Developed by Sajjad Baghaee  ·  GitHub',bg=THEME['bg'],
                     fg=THEME['accent'],cursor='hand2',font=(FONT,9,'underline'))
        devlink.pack(side='right')
        devlink.bind('<Button-1>',lambda e:webbrowser.open(AUTHOR_GITHUB))
        self._update_broker_label()

    def _make_overview(self):
        make_label(self.overview,'Fleet overview',23,True).pack(anchor='w',pady=(0,6))
        make_label(self.overview,'Multiple MQTT servers • controls in every row • click a device name to configure',10,muted=True).pack(anchor='w',pady=(0,14))
        metrics=tk.Frame(self.overview,bg=THEME['bg']);metrics.pack(fill='x',pady=(0,14))
        self.overview_metrics={}
        for idx,title in enumerate(['DEVICES','REPORTING','SWITCHES ON','TOTAL LIVE POWER']):
            metrics.grid_columnconfigure(idx,weight=1,uniform='kpi')
            card=tk.Frame(metrics,bg=THEME['surface'],padx=13,pady=13)
            card.grid(row=0,column=idx,sticky='ew',padx=(0,8 if idx<3 else 0))
            make_label(card,title,9,True,muted=True).pack(anchor='w')
            lbl=make_label(card,'—',19,True);lbl.pack(anchor='w',pady=(8,0))
            self.overview_metrics[title]=lbl
        panel=tk.Frame(self.overview,bg=THEME['surface'],padx=11,pady=11)
        panel.pack(fill='both',expand=True,pady=(0,12))
        self.columns=['DEVICE','BROKER','STATE','POWER','ENERGY','LAST UPDATE','CONTROLS']
        self.weights=[4,2,1,2,2,2,5]
        header=tk.Frame(panel,bg=THEME['surface2']);header.pack(fill='x')
        for i,title in enumerate(self.columns):
            header.grid_columnconfigure(i,weight=self.weights[i],uniform='fleet')
            tk.Label(header,text=title,bg=THEME['surface2'],fg=THEME['fg'],
              font=(FONT,9,'bold'),padx=4,pady=9).grid(row=0,column=i,sticky='ew')
        wrap=tk.Frame(panel,bg=THEME['surface']);wrap.pack(fill='both',expand=True)
        self.rows_canvas=tk.Canvas(wrap,bg=THEME['surface'],highlightthickness=0)
        scroll=tk.Scrollbar(wrap,command=self.rows_canvas.yview)
        scroll.pack(side='right',fill='y');self.rows_canvas.pack(side='left',fill='both',expand=True)
        self.rows_canvas.configure(yscrollcommand=scroll.set)
        self.rows_inner=tk.Frame(self.rows_canvas,bg=THEME['surface'])
        rwin=self.rows_canvas.create_window((0,0),window=self.rows_inner,anchor='nw')
        self.rows_inner.bind('<Configure>',lambda e:self.rows_canvas.configure(scrollregion=self.rows_canvas.bbox('all')))
        self.rows_canvas.bind('<Configure>',lambda e:self.rows_canvas.itemconfigure(rwin,width=e.width))
        logpane=tk.Frame(self.overview,bg=THEME['surface'],padx=11,pady=9)
        logpane.pack(fill='x')
        make_label(logpane,'ACTIVITY LOG',10,True).pack(anchor='w',pady=(0,5))
        self.log_box=tk.Text(logpane,height=4,bg=THEME['entry'],fg=THEME['muted'],
                 font=('Consolas',9),relief='flat',state='disabled')
        self.log_box.pack(fill='x')
        self.update_overview()

    def _create_row(self,d):
        bg=THEME['surface'] if len(self.row_widgets)%2==0 else THEME['surface2']
        row=tk.Frame(self.rows_inner,bg=bg);row.pack(fill='x',pady=(0,2))
        for i,w in enumerate(self.weights):row.grid_columnconfigure(i,weight=w,uniform='fleet')
        link=tk.Button(row,text=d.name,command=lambda uid=d.uid:self.show_device(uid),
             bg=bg,fg=THEME['accent'],activebackground=bg,activeforeground='white',
             font=(FONT,9,'bold'),relief='flat',cursor='hand2',anchor='w',padx=6,pady=9)
        link.grid(row=0,column=0,sticky='ew')
        fields={}
        for name,col in [('broker',1),('state',2),('power',3),('energy',4),('last',5)]:
            t=tk.Label(row,bg=bg,fg=THEME['fg'],font=(FONT,9),anchor='center')
            t.grid(row=0,column=col,sticky='ew');fields[name]=t
        ctr=tk.Frame(row,bg=bg);ctr.grid(row=0,column=6,sticky='ew')
        controls={}
        for cmd,caption,tone in [('on','ON','green'),('off','OFF','red'),('toggle','TOGGLE','accent')]:
            b=button(ctr,caption,lambda uid=d.uid,c=cmd:self.send_command(uid,c),tone,5)
            b.configure(font=(FONT,8,'bold'),padx=4)
            b.pack(side='left',fill='x',expand=True,padx=2)
            controls[cmd]=b
        self.row_widgets[d.uid]={'row':row,'name':link,'fields':fields,'controls':controls}

    def log(self,text):
        line=f'[{datetime.now().strftime("%H:%M:%S")}] {text}'
        self.log_lines.append(line)
        if hasattr(self,'log_box'):
            self.log_box.configure(state='normal')
            self.log_box.insert('end',line+'\n')
            if float(self.log_box.index('end-1c'))>240:self.log_box.delete('1.0','25.0')
            self.log_box.see('end')
            self.log_box.configure(state='disabled')

    def _update_broker_label(self):
        total=len(self.sessions)
        count=sum(1 for b in self.sessions if self.is_connected(b))
        if not self.want_connected:
            text,fg='● DISCONNECTED',THEME['muted']
        elif count==total and total:
            text,fg=f'● {count}/{total} BROKERS CONNECTED',THEME['green']
        elif count:
            text,fg=f'● {count}/{total} BROKERS CONNECTED',THEME['amber']
        else:
            text,fg=f'● CONNECTING ({total})',THEME['amber']
        self.broker_lbl.configure(text=text,fg=fg)
        self.connect_btn.configure(text='Disconnect all' if self.want_connected else 'Connect all',
                bg=THEME['red'] if self.want_connected else THEME['accent'])

    def _sidebar_state(self,d):
        # Relay state and device MQTT presence are independent.
        relay_color=(THEME['green'] if d.state is True else
                     THEME['red'] if d.state is False else THEME['muted'])
        broker_reachable=self.is_connected(d.broker_id)
        if not broker_reachable:
            mqtt_color,mqtt_text=THEME['red'],'Broker disconnected'
        elif d.mqtt_online is True:
            mqtt_color,mqtt_text=THEME['green'],'Plug MQTT online'
        elif d.mqtt_online is False:
            mqtt_color,mqtt_text=THEME['red'],'Plug MQTT offline'
        else:
            mqtt_color,mqtt_text=THEME['muted'],'Plug MQTT unknown'
        last='Last ON' if d.state is True else 'Last OFF' if d.state is False else 'State unknown'
        fresh=d.fresh() and broker_reachable and d.mqtt_online is not False
        stale='' if fresh else ' (stale)'
        watts=f'{d.power:.1f} W' if d.power is not None else '— W'
        return relay_color,mqtt_color,f'{last}{stale}  ·  {watts}',mqtt_text

    def _update_sidebar_device(self,uid):
        d=self.devices.get(uid)
        row=self.sidebar_indicators.get(uid)
        if d is None or not row:return
        if not row['frame'].winfo_exists():return
        rcolor,mcolor,last,mqtt_status=self._sidebar_state(d)
        row['relay'].configure(fg=rcolor)
        row['mqtt'].configure(fg=mcolor)
        row['name'].configure(text=d.name)
        row['details'].configure(text=f'{last}\n{mqtt_status}  ·  {self.brokers[d.broker_id].name}')

    def draw_sidebar(self):
        for w in self.side_inner.winfo_children():w.destroy()
        self.sidebar_buttons.clear()
        self.sidebar_indicators.clear()
        query=self.search_var.get().casefold().strip()
        for d in self.devices.values():
            if query and query not in (d.name+' '+d.prefix+' '+self.brokers[d.broker_id].name).casefold():continue
            active=d.uid==self.selected_uid
            bg=THEME['surface2'] if active else THEME['surface']
            frame=tk.Frame(self.side_inner,bg=bg,padx=7,pady=7)
            frame.pack(fill='x',pady=(0,3),padx=4)
            head=tk.Frame(frame,bg=bg);head.pack(fill='x')
            relay=tk.Label(head,text='●',font=(FONT,14,'bold'),bg=bg,fg=THEME['muted'],padx=1)
            relay.pack(side='left')
            mqtt_status=tk.Label(head,text='●',font=(FONT,14,'bold'),bg=bg,fg=THEME['muted'],padx=2)
            mqtt_status.pack(side='left')
            name=tk.Button(head,text=d.name,
                command=lambda uid=d.uid:self.show_device(uid),
                bg=bg,fg=THEME['accent'] if active else THEME['fg'],
                activebackground=bg,activeforeground=THEME['accent'],
                font=(FONT,9,'bold'),relief='flat',anchor='w',
                padx=5,pady=2,cursor='hand2')
            name.pack(side='left',fill='x',expand=True)
            details=tk.Label(frame,bg=bg,fg=THEME['muted'],font=(FONT,8),
                anchor='w',justify='left',padx=6,pady=1)
            details.pack(fill='x')
            # Entire device entry opens its page, not only the device name.
            for target in (frame,relay,mqtt_status,details):
                target.bind('<Button-1>',lambda e,uid=d.uid:self.show_device(uid))
                target.configure(cursor='hand2')
            self.sidebar_buttons[d.uid]=name
            self.sidebar_indicators[d.uid]={'frame':frame,'relay':relay,
                'mqtt':mqtt_status,'name':name,'details':details}
            self._update_sidebar_device(d.uid)
        self.device_count.configure(text=f'DEVICES  {len(self.devices)}/{MAX_DEVICES}')

    def update_overview(self):
        if not hasattr(self,'rows_inner'):return
        total=len(self.devices)
        reporting=[d for d in self.devices.values() if self.is_connected(d.broker_id) and d.fresh() and d.mqtt_online is not False]
        on=sum(d.state is True for d in reporting)
        watts=sum(max(0,d.power) for d in reporting if d.power is not None)
        for k,v in [('DEVICES',str(total)),('REPORTING',f'{len(reporting)}/{total}'),
                    ('SWITCHES ON',str(on)),('TOTAL LIVE POWER',f'{watts:,.1f} W')]:
            self.overview_metrics[k].configure(text=v)
        for uid in list(self.row_widgets):
            if uid not in self.devices:self.row_widgets.pop(uid)['row'].destroy()
        for d in self.devices.values():
            if d.uid not in self.row_widgets:self._create_row(d)
            row=self.row_widgets[d.uid]
            ready=self.is_connected(d.broker_id) and d.fresh() and d.mqtt_online is not False
            has_sub=d.uid in self.subscribed
            known=ready and d.state is not None
            state='ON' if known and d.state else 'OFF' if known else 'STALE'
            row['name'].configure(text=d.name)
            f=row['fields']
            f['broker'].configure(text=self.brokers[d.broker_id].name,
                fg=THEME['green'] if self.is_connected(d.broker_id) else THEME['red'])
            f['state'].configure(text=state,fg=THEME['green'] if state=='ON' else THEME['red'] if state=='OFF' else THEME['amber'])
            f['power'].configure(text=metric(d.power,'W') if ready else '—')
            f['energy'].configure(text=metric(d.energy,'Wh'))
            f['last'].configure(text=d.last_time[11:19] if d.last_time!='Never' else 'Never')
            for b in row['controls'].values():
                b.configure(state='normal' if known and has_sub and d.mqtt_online is not False else 'disabled')

    def refresh_views(self):
        for uid in self.devices:
            self._update_sidebar_device(uid)
        for page in self.pages.values():page.refresh()
        self.update_overview()
        self._update_broker_label()

    def show_overview(self):
        self.selected_uid=None
        for page in self.pages.values():page.pack_forget()
        self.overview.pack(fill='both',expand=True)
        self.draw_sidebar();self.update_overview()

    def show_device(self,uid):
        d=self.devices.get(uid)
        if not d:return
        self.selected_uid=uid
        self.overview.pack_forget()
        for page in self.pages.values():page.pack_forget()
        if uid not in self.pages:self.pages[uid]=DevicePage(self.content,self,d)
        page=self.pages.pop(uid);self.pages[uid]=page
        while len(self.pages)>MAX_CACHED_PAGES:
            first=next(iter(self.pages));self.pages.pop(first).destroy()
        page.pack(fill='both',expand=True)
        page.sync_broker();page.refresh();self.draw_sidebar()

    def add_device(self):
        dlg=DeviceDialog(self);self.wait_window(dlg)
        if not dlg.result:return
        d=dlg.result
        self.devices[d.uid]=d
        self._rebuild_index();self.save_config();self.draw_sidebar();self.show_device(d.uid)
        self.log(f'Added {d.name} on broker {self.brokers[d.broker_id].name}.')
        if self.want_connected:
            self._connect_profile(d.broker_id)
            if self.is_connected(d.broker_id):self._subscribe(d)

    def edit_device(self,uid):
        old=self.devices.get(uid)
        if not old:return
        dlg=DeviceDialog(self,old);self.wait_window(dlg)
        if not dlg.result:return
        new=dlg.result
        moved=old.broker_id!=new.broker_id or old.prefix!=new.prefix or old.switch_id!=new.switch_id
        if moved:self._unsubscribe(old)
        if not moved:
            for k in ['state','power','voltage','current','energy','last_seen','last_time','history']:
                setattr(new,k,getattr(old,k))
        self.devices[uid]=new
        self._rebuild_index()
        page=self.pages.pop(uid,None)
        if page:page.destroy()
        self.save_config();self.show_device(uid)
        if moved and self.want_connected:
            self._connect_profile(new.broker_id)
            if self.is_connected(new.broker_id):self._subscribe(new)
        self.log(f'Updated device {new.name}.')

    def assign_broker(self,uid,bid):
        d=self.devices.get(uid)
        if not d or bid not in self.brokers:return
        if d.broker_id==bid:
            self.log(f'{d.name} already assigned to {self.brokers[bid].name}.')
            return
        if any(x.uid!=uid and x.broker_id==bid and x.prefix==d.prefix and x.switch_id==d.switch_id for x in self.devices.values()):
            messagebox.showerror('Duplicate topic','This broker already has a device with the same prefix and channel.',parent=self)
            if uid in self.pages:self.pages[uid].sync_broker()
            return
        previous=self.brokers[d.broker_id].name
        self._unsubscribe(d)
        d.broker_id=bid
        self._invalidate(d)
        self._rebuild_index();self.save_config()
        self.log(f'{d.name} moved from {previous} to {self.brokers[bid].name}.')
        if self.want_connected:
            self._connect_profile(bid)
            if self.is_connected(bid):self._subscribe(d)
        if uid in self.pages:self.pages[uid].sync_broker()
        self.draw_sidebar();self.refresh_views()

    @staticmethod
    def _invalidate(d):
        d.state=d.power=d.voltage=d.current=d.energy=None
        d.mqtt_online=None
        d.last_seen=0.0;d.last_time='Never'
        d.history.clear()

    def remove_device(self,uid):
        d=self.devices.get(uid)
        if not d:return
        if not messagebox.askyesno('Remove device',f'Remove {d.name} from this application?\nThe physical plug will not change.',parent=self):return
        self._unsubscribe(d)
        self.devices.pop(uid,None)
        page=self.pages.pop(uid,None)
        if page:page.destroy()
        self._rebuild_index();self.save_config();self.show_overview()
        self.log(f'Removed device {d.name}.')

    def export_history(self,uid):
        d=self.devices.get(uid)
        if not d:return
        if not d.history:
            messagebox.showinfo('No history','No power samples are available for this session.',parent=self)
            return
        filename=filedialog.asksaveasfilename(parent=self,defaultextension='.csv',
             initialfile=re.sub(r'[^A-Za-z0-9_-]','_',d.name)+'_power.csv',
             filetypes=[('CSV','*.csv')])
        if filename:
            try:
                with open(filename,'w',encoding='utf-8',newline='') as f:
                    writer=csv.writer(f);writer.writerow(['timestamp_local','power_watts'])
                    writer.writerows(d.history)
                self.log(f'Power CSV exported for {d.name}.')
            except OSError as exc:
                messagebox.showerror('CSV export failed',str(exc),parent=self)

    def apply_broker(self,p,password):
        was=self.brokers.get(p.bid)
        old_password=self.passwords.get(p.bid,'')
        self.brokers[p.bid]=p
        self.passwords[p.bid]=password
        if not self.save_config():
            if was:self.brokers[p.bid]=was
            else:self.brokers.pop(p.bid,None)
            self.passwords[p.bid]=old_password
            return False
        if p.remember and keyring is not None and password:
            threading.Thread(target=self._store_password,args=(p.bid,password),daemon=True).start()
        elif was and was.remember and not p.remember and keyring is not None:
            threading.Thread(target=self._delete_password,args=(p.bid,),daemon=True).start()
        for page in self.pages.values():page.sync_broker()
        self.log(f'Broker profile saved: {p.name}.')
        if self.want_connected:
            # Change only the impacted profile, preserving every other broker.
            if p.bid in self.sessions:self._disconnect_profile(p.bid)
            if p.bid=='default' and not self.devices or any(d.broker_id==p.bid for d in self.devices.values()):
                self._connect_profile(p.bid)
        self.refresh_views()
        return True

    def delete_broker(self,bid):
        if bid=='default' or bid not in self.brokers:return
        if any(d.broker_id==bid for d in self.devices.values()):return
        self._disconnect_profile(bid)
        self.brokers.pop(bid);self.passwords.pop(bid,None)
        if keyring is not None:
            threading.Thread(target=self._delete_password,args=(bid,),daemon=True).start()
        self.save_config();self.refresh_views()

    def _retrieve_password(self,bid):
        try:
            value=keyring.get_password(PASSWORD_SERVICE,bid)
            if value:self.events.put((bid,None,'password_loaded',value))
        except Exception:
            pass

    @staticmethod
    def _store_password(bid,password):
        try:keyring.set_password(PASSWORD_SERVICE,bid,password)
        except Exception:pass

    @staticmethod
    def _delete_password(bid):
        try:keyring.delete_password(PASSWORD_SERVICE,bid)
        except Exception:pass

    def toggle_connections(self):
        if self.want_connected:self.disconnect_all()
        else:self.connect_all()

    def connect_all(self):
        wanted={d.broker_id for d in self.devices.values()}
        if not wanted:wanted={'default'}
        profiles=[self.brokers[x] for x in self.brokers if x in wanted]
        errors=[]
        valid=[]
        for p in profiles:
            try:
                check_profile(p)
                valid.append(p)
            except ValueError as exc:
                errors.append(f'{p.name}: {exc}')
        if errors:
            messagebox.showwarning('Some broker profiles need settings',
                'These brokers were skipped:\n'+'\n'.join(errors)+
                '\n\nOther correctly configured brokers can still connect.',parent=self)
        if not valid:return
        profiles=valid
        plain=[p.name for p in profiles if p.mode=='plain']
        if plain:
            if not messagebox.askyesno('Unencrypted MQTT connections',
                'These profiles use unencrypted MQTT:\n'+', '.join(plain)+
                '\n\nCredentials and commands are not protected on the network.\nUse trusted networks or VPNs only. Connect anyway?',
                icon='warning',parent=self):return
        self.want_connected=True
        self.approved_plain={p.bid for p in profiles if p.mode=='plain'}
        for p in profiles:self._connect_profile(p.bid)
        self._update_broker_label()

    def _connect_profile(self,bid):
        if bid in self.sessions or not self.want_connected:return
        p=self.brokers.get(bid)
        if not p:return
        if p.mode=='plain' and bid not in self.approved_plain:
            if not messagebox.askyesno('Unencrypted MQTT',
                f'Broker {p.name} uses plain MQTT. Credentials and commands are unencrypted.\n\nConnect only on a trusted network or VPN. Continue?',
                icon='warning', parent=self):
                self.log(f'Plain MQTT connection cancelled for {p.name}.')
                return
            self.approved_plain.add(bid)
        try:
            host,port=check_profile(p)
            cli=mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                 client_id=f'shelly-fleet-{uuid.uuid4().hex[:16]}',protocol=mqtt.MQTTv311)
            if p.username:
                cli.username_pw_set(p.username,self.passwords.get(bid,''))
            if p.mode=='tls':
                ctx=ssl.create_default_context(cafile=p.ca if p.ca.strip() else None)
                ctx.minimum_version=ssl.TLSVersion.TLSv1_2
                cli.tls_set_context(ctx)
            cli.reconnect_delay_set(min_delay=1,max_delay=30)
        except (OSError,ValueError,ssl.SSLError) as exc:
            self.log(f'Connection setup failed for {p.name}: {type(exc).__name__}: {exc}')
            return
        sid=uuid.uuid4().hex
        # MQTT thread callbacks only place events in the queue; no Tk calls off main thread.
        def connected(c,userdata,flags,reason_code,properties):
            kind='rejected' if reason_code.is_failure else 'connected'
            self.events.put((bid,sid,kind,str(reason_code)))
        def disconnected(c,userdata,flags,reason_code,properties):
            if reason_code.is_failure:self.events.put((bid,sid,'lost',str(reason_code)))
        def subscribed(c,userdata,mid,reason_codes,properties):
            self.events.put((bid,sid,'subscribed',(mid,[not x.is_failure for x in reason_codes])))
        def received(c,userdata,msg):
            key=(bid,msg.topic)
            online_uids=self.online_index.get(key)
            if online_uids:
                value=msg.payload.decode('utf-8',errors='replace').strip().lower()
                if value in ('true','false','1','0'):
                    self.events.put((bid,sid,'online',(tuple(online_uids),value in ('true','1'))))
                return
            uid=self.status_index.get(key)
            kind='status'
            if uid is None:
                uid=self.error_index.get(key);kind='device_error'
            if uid is None:return
            try:
                payload=json.loads(msg.payload.decode('utf-8')) if kind=='status' else msg.payload.decode('utf-8',errors='replace')[:300]
            except (ValueError,UnicodeError):return
            self.events.put((bid,sid,kind,(uid,payload,bool(getattr(msg,'retain',False)))))
        cli.on_connect=connected;cli.on_disconnect=disconnected
        cli.on_subscribe=subscribed;cli.on_message=received
        self.sessions[bid]={'client':cli,'token':sid,'ready':False,'waiting':{}}
        self.log(f'Connecting broker {p.name} ({MODES[p.mode]}, port {port})…')
        threading.Thread(target=self._connect_worker,args=(bid,sid,cli,host,port),daemon=True).start()
        self._update_broker_label()

    def _connect_worker(self,bid,sid,cli,host,port):
        try:
            cli.connect(host,port,keepalive=60)
            cli.loop_start()
        except Exception as exc:
            self.events.put((bid,sid,'connect_error',f'{type(exc).__name__}: {exc}'))

    def _disconnect_profile(self,bid):
        sess=self.sessions.pop(bid,None)
        for d in self.devices.values():
            if d.broker_id==bid:
                self.subscribed.discard(d.uid);self.failed_subs.discard(d.uid)
                d.mqtt_online=None
        if sess:
            threading.Thread(target=self._cleanup,args=(sess['client'],),daemon=True).start()
        self._update_broker_label()

    @staticmethod
    def _cleanup(client):
        try:client.disconnect();client.loop_stop()
        except Exception:pass

    def disconnect_all(self):
        self.want_connected=False
        self.approved_plain.clear()
        for bid in list(self.sessions):self._disconnect_profile(bid)
        self.log('All MQTT broker sessions disconnected.')
        self.refresh_views()

    def _subscribe(self,d):
        sess=self.sessions.get(d.broker_id)
        if not sess or not sess['ready']:return
        try:
            code,mid=sess['client'].subscribe([(d.status_topic,1),(d.error_topic,1),(d.online_topic,1)])
            if code==mqtt.MQTT_ERR_SUCCESS:
                sess['waiting'][mid]=d.uid
            else:
                self.failed_subs.add(d.uid)
                self.log(f'Subscription failed for {d.name}: {mqtt.error_string(code)}')
        except Exception as exc:
            self.failed_subs.add(d.uid)
            self.log(f'Subscription failed for {d.name}: {exc}')

    def _unsubscribe(self,d):
        sess=self.sessions.get(d.broker_id)
        if sess and sess['ready']:
            topics=[d.status_topic,d.error_topic]
            if not any(x.uid!=d.uid and x.broker_id==d.broker_id and x.online_topic==d.online_topic
                       for x in self.devices.values()):
                topics.append(d.online_topic)
            try:sess['client'].unsubscribe(topics)
            except Exception:pass
        if sess:
            for mid,uid in list(sess['waiting'].items()):
                if uid==d.uid:sess['waiting'].pop(mid,None)
        self.subscribed.discard(d.uid);self.failed_subs.discard(d.uid)

    def send_command(self,uid,command,quiet=False):
        d=self.devices.get(uid)
        if not d:return
        sess=self.sessions.get(d.broker_id)
        if not sess or not sess['ready'] or uid not in self.subscribed:
            if not quiet:self.log(f'{d.name} is not subscribed on its assigned broker.')
            return
        if command!='status_update' and (not d.fresh() or d.state is None or d.mqtt_online is False):
            if not quiet:messagebox.showwarning('No fresh status',
                f'{d.name} has not recently reported its switch state. Refresh its status first.',parent=self)
            return
        if command not in ('on','off','toggle','status_update'):return
        try:
            result=sess['client'].publish(d.command_topic,command,qos=1,retain=False)
            if result.rc!=mqtt.MQTT_ERR_SUCCESS:
                self.log(f'Publish failed for {d.name}: {mqtt.error_string(result.rc)}')
            elif not quiet:self.log(f'Sent {command} to {d.name} via {self.brokers[d.broker_id].name}; awaiting feedback.')
        except Exception as exc:
            self.log(f'Publish failed for {d.name}: {exc}')

    def _poll_events(self):
        if self.closing:return
        for _ in range(250):
            try:bid,sid,kind,payload=self.events.get_nowait()
            except queue.Empty:break
            if kind=='password_loaded':
                if bid in self.brokers and self.brokers[bid].remember and not self.passwords.get(bid):
                    self.passwords[bid]=payload
                    self.log(f'Credential loaded for broker {self.brokers[bid].name}.')
                continue
            sess=self.sessions.get(bid)
            if not sess or sess['token']!=sid:continue
            brokername=self.brokers[bid].name
            if kind=='connected':
                sess['ready']=True;sess['waiting'].clear()
                for d in self.devices.values():
                    if d.broker_id==bid:
                        self.subscribed.discard(d.uid)
                        d.mqtt_online=None
                        self._subscribe(d)
                self.log(f'Broker connected: {brokername}.')
            elif kind=='subscribed':
                mid,grants=payload
                uid=sess['waiting'].pop(mid,None)
                d=self.devices.get(uid)
                if not d or d.broker_id!=bid:continue
                if grants and grants[0]:
                    self.subscribed.add(uid);self.failed_subs.discard(uid)
                    self.send_command(uid,'status_update',quiet=True)
                    self.log(f'Subscribed to {d.name} via {brokername}.')
                else:
                    self.failed_subs.add(uid);self.log(f'Access denied for status topic of {d.name}.')
                if len(grants)>1 and not grants[1]:
                    self.log(f'Error topic subscription denied for {d.name}.')
                if len(grants)>2 and not grants[2]:
                    self.log(f'Online/Last-Will topic subscription denied for {d.name}.')
            elif kind=='status':
                uid,data,retained=payload
                d=self.devices.get(uid)
                if d and d.broker_id==bid and d.update(data):
                    if retained and d.mqtt_online is not True:
                        # Retained telemetry is not proof of a live, current measurement.
                        d.last_seen=0.0
                    if not retained:d.mqtt_online=True  # a new packet proves a live device session
                    self._repaint(uid)
            elif kind=='online':
                uids,online=payload
                for uid in uids:
                    d=self.devices.get(uid)
                    if d and d.broker_id==bid:
                        d.mqtt_online=online
                        self._repaint(uid)
            elif kind=='device_error':
                uid,data,_retained=payload
                d=self.devices.get(uid)
                if d and d.broker_id==bid:self.log(f'Device error from {d.name}: {data}')
            elif kind=='lost':
                sess['ready']=False;sess['waiting'].clear()
                for d in self.devices.values():
                    if d.broker_id==bid:
                        self.subscribed.discard(d.uid)
                        d.mqtt_online=None
                self.log(f'Broker {brokername} disconnected: {payload}; retrying automatically.')
            elif kind in ('rejected','connect_error'):
                self.log(f'Broker {brokername} connection failed: {payload}')
                self._disconnect_profile(bid)
            self._update_broker_label()
        self._after_tasks.append(self.after(150,self._poll_events))

    def _repaint(self,uid):
        d=self.devices.get(uid)
        if d:
            self._update_sidebar_device(uid)
            if uid in self.pages:self.pages[uid].refresh()
        self.update_overview()

    def _maintenance(self):
        if self.closing:return
        self.refresh_views()
        self._after_tasks.append(self.after(5000,self._maintenance))

    def _auto_refresh(self):
        if self.closing:return
        if self.want_connected:
            for i,uid in enumerate(tuple(self.devices)):
                self._after_tasks.append(self.after(i*100,lambda u=uid:self.send_command(u,'status_update',quiet=True)))
        self._after_tasks.append(self.after(REFRESH_INTERVAL_MS,self._auto_refresh))

    def show_about(self):
        dlg=tk.Toplevel(self);dlg.title(f'About Shelly Fleet {APP_VERSION}')
        dlg.geometry('740x740');dlg.minsize(620,650)
        dlg.configure(bg=THEME['surface']);dlg.transient(self);dlg.grab_set()
        area=tk.Frame(dlg,bg=THEME['surface'],padx=27,pady=22);area.pack(fill='both',expand=True)
        make_label(area,f'SHELLY FLEET  v{APP_VERSION}',22,True).pack(anchor='w')
        make_label(area,'Independent open-source MQTT fleet controller',11,muted=True).pack(anchor='w',pady=(5,15))
        self._about_para(area,'WHAT IT DOES',
           'A desktop controller for up to 100 MQTT-operated Shelly plugs. Displays each device’s ON/OFF status, live power, voltage, current, total energy, power chart and CSV export. The overview provides direct per-device ON/OFF/TOGGLE buttons.')
        self._about_para(area,'MULTI-BROKER DESIGN',
           'Each device uses the Default broker unless another named profile is selected. Independent MQTT connections can run simultaneously using TLS or plain MQTT, with separate hosts, credentials, CAs and per-mode ports. A broker problem does not automatically disconnect other profiles.')
        self._about_para(area,'SECURITY AND LOCAL DATA',
           'TLS verification remains enabled; unencrypted MQTT is explicitly warned about. Broker passwords are never saved into the JSON configuration. Optional OS credential storage uses keyring. Live charts are limited to the current session; remote command delivery is confirmed by device status, not merely by publish acceptance.')
        self._about_para(area,'COMPATIBILITY & LIMITS',
           'Designed and tested offline for Shelly Plug S Gen3 MQTT switch status and command topics. Up to 100 registered devices; this is not a cloud service, long-term historian, billing-grade meter or an official Shelly application.')
        make_label(area,'DEVELOPED BY',10,True).pack(anchor='w',pady=(8,0))
        make_label(area,AUTHOR_NAME,13,True).pack(anchor='w',pady=(4,0))
        link=tk.Label(area,text=AUTHOR_GITHUB,bg=THEME['surface'],fg=THEME['accent'],
                font=(FONT,11,'underline'),cursor='hand2')
        link.pack(anchor='w',pady=(3,9));link.bind('<Button-1>',lambda e:webbrowser.open(AUTHOR_GITHUB))
        make_label(area,'Technology: Python • Tkinter • Eclipse Paho MQTT • optional OS keyring',9,muted=True).pack(anchor='w')
        foot=tk.Frame(area,bg=THEME['surface']);foot.pack(fill='x',side='bottom',pady=(15,0))
        button(foot,'Close',dlg.destroy,'quiet').pack(side='right')
        button(foot,'Open Help',lambda:(dlg.destroy(),self.show_help())).pack(side='right',padx=(0,8))

    @staticmethod
    def _about_para(parent,heading,text):
        make_label(parent,heading,10,True).pack(anchor='w',pady=(0,4))
        tk.Label(parent,text=text,bg=THEME['surface'],fg=THEME['muted'],
             wraplength=650,justify='left',anchor='w',font=(FONT,10)).pack(fill='x',pady=(0,12))


    @staticmethod
    def _insert_status_help(editor):
        """Render a readable, accessible status key inside Help → Devices & Data."""
        panel=tk.Frame(editor,bg=THEME['surface2'],padx=15,pady=13,
                       highlightbackground=THEME['line'],highlightthickness=1)
        make_label(panel,'STATUS INDICATORS  ·  LEFT SIDEBAR',11,True).pack(anchor='w',pady=(0,12))
        grid=tk.Frame(panel,bg=THEME['surface2']);grid.pack(fill='x')
        headings=('INDICATOR','GREEN','RED','GRAY')
        for col,title in enumerate(headings):
            grid.grid_columnconfigure(col,weight=3 if col==0 else 2,minsize=120 if col==0 else 110)
            tk.Label(grid,text=title,bg=THEME['surface2'],fg=THEME['muted'],
                font=(FONT,9,'bold'),anchor='w').grid(row=0,column=col,sticky='w',padx=8,pady=(0,9))
        for row,(label,detail,values) in enumerate((
            ('RELAY','Last switch state',(('ON','green'),('OFF','red'),('Unknown','muted'))),
            ('PLUG MQTT','Device presence',(('Online','green'),('Offline','red'),('Unknown','muted'))),
        ),start=1):
            cell=tk.Frame(grid,bg=THEME['surface2']);cell.grid(row=row,column=0,sticky='w',padx=8,pady=6)
            tk.Label(cell,text=label,bg=THEME['surface2'],fg=THEME['fg'],font=(FONT,10,'bold')).pack(anchor='w')
            tk.Label(cell,text=detail,bg=THEME['surface2'],fg=THEME['muted'],font=(FONT,9)).pack(anchor='w')
            for col,(value,tone) in enumerate(values,start=1):
                cell=tk.Frame(grid,bg=THEME['surface2']);cell.grid(row=row,column=col,sticky='w',padx=8,pady=6)
                tk.Label(cell,text='●',bg=THEME['surface2'],fg=THEME[tone],
                    font=(FONT,13,'bold')).pack(side='left',padx=(0,5))
                tk.Label(cell,text=value,bg=THEME['surface2'],fg=THEME['fg'],
                    font=(FONT,10)).pack(side='left')
        tk.Label(panel,text='Indicators describe different signals; neither substitutes for a fresh device reading.',
                 bg=THEME['surface2'],fg=THEME['muted'],font=(FONT,9),
                 wraplength=740,justify='left',anchor='w').pack(fill='x',pady=(12,0))
        editor.window_create('end',window=panel)
        editor.insert('end','\n\n')

    def show_help(self, initial_tab='Quick Start'):
        dlg=tk.Toplevel(self);dlg.title(f'Shelly Fleet — Help & User Guide v{APP_VERSION}')
        dlg.geometry('1110x770');dlg.minsize(940,550)
        dlg.configure(bg=THEME['surface']);dlg.transient(self);dlg.grab_set()
        head=tk.Frame(dlg,bg=THEME['surface'],padx=19,pady=14);head.pack(fill='x')
        make_label(head,'Shelly Fleet — Help & User Guide',18,True).pack(anchor='w')
        make_label(head,'Installation • device configuration • multiple brokers • monitoring • troubleshooting',10,muted=True).pack(anchor='w',pady=(4,0))
        tabs=tk.Frame(dlg,bg=THEME['surface'],padx=17,pady=5);tabs.pack(fill='x')
        page=tk.Frame(dlg,bg=THEME['entry']);page.pack(fill='both',expand=True,padx=18,pady=(5,8))
        editor=tk.Text(page,bg=THEME['entry'],fg=THEME['fg'],relief='flat',
             wrap='word',font=(FONT,10),padx=19,pady=16,spacing2=3,spacing3=8)
        scroll=tk.Scrollbar(page,command=editor.yview);scroll.pack(side='right',fill='y')
        editor.pack(side='left',fill='both',expand=True)
        editor.configure(yscrollcommand=scroll.set)
        editor.tag_configure('heading',foreground=THEME['accent'],font=(FONT,12,'bold'),spacing1=11,spacing3=5)
        editor.tag_configure('body',foreground=THEME['fg'],font=(FONT,10))
        editor.tag_configure('code',foreground='#83D8C7',font=('Consolas',10))
        editor.tag_configure('notice',foreground=THEME['amber'],font=(FONT,10,'bold'))
        tab_buttons={}
        def select(tab):
            editor.configure(state='normal');editor.delete('1.0','end')
            for kind,content in HELP_CONTENT[tab]:
                if kind=='status_table':self._insert_status_help(editor)
                else:editor.insert('end',content+'\n\n',kind)
            editor.configure(state='disabled');editor.yview_moveto(0)
            for name,b in tab_buttons.items():
                current=name==tab
                # Native Tk buttons, not themed tabs, to guarantee visible text on Windows.
                b.configure(bg=THEME['accent'] if current else THEME['surface2'],
                            fg='#FFFFFF' if current else THEME['fg'],
                            activebackground=THEME['accent'] if current else THEME['surface2'],
                            activeforeground='#FFFFFF')
        for name in HELP_CONTENT:
            b=tk.Button(tabs,text=name,command=lambda x=name:select(x),
                 relief='flat',font=(FONT,9,'bold'),cursor='hand2',
                 bg=THEME['surface2'],fg=THEME['fg'],
                 activebackground=THEME['accent'],activeforeground='#FFFFFF',
                 padx=12,pady=10)
            b.pack(side='left',padx=(0,3));tab_buttons[name]=b
        foot=tk.Frame(dlg,bg=THEME['surface'],padx=18,pady=11);foot.pack(side='bottom',fill='x')
        button(foot,'Official Shelly MQTT documentation',lambda:webbrowser.open(DOCS_URL),'quiet',6).pack(side='left')
        button(foot,'Developer GitHub',lambda:webbrowser.open(AUTHOR_GITHUB),'quiet',6).pack(side='left',padx=(8,0))
        button(foot,'Close',dlg.destroy,'accent',6).pack(side='right')
        select(initial_tab if initial_tab in HELP_CONTENT else 'Quick Start')
        dlg.bind('<Escape>',lambda e:dlg.destroy())
        return dlg

    def _close(self):
        self.closing=True
        for job in self._after_tasks:
            try:self.after_cancel(job)
            except tk.TclError:pass
        self._after_tasks.clear()
        self.save_config()
        self.want_connected=False
        sessions=list(self.sessions.values());self.sessions.clear()
        for sess in sessions:
            try:sess['client'].disconnect();sess['client'].loop_stop()
            except Exception:pass
        self.destroy()


if __name__=='__main__':
    ShellyFleetApp().mainloop()
