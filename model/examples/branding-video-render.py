#!/usr/bin/env python3
"""Illustrative, synthetic warehouse footage. No real analytics is claimed.

Produces an untouched synthetic source and a separate annotated H.264 export.
The names come from the verified VL Exchange attribution demo event. Drawing
and positions are simulated; this is not a video analytics implementation.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont

EXAMPLES = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, required=True,
                    help='new or empty directory for generated videos and previews')
parser.add_argument('--font', help='path to a regular TrueType font (optional)')
parser.add_argument('--bold-font', help='path to a bold TrueType font (optional)')
args = parser.parse_args()
DIR = args.output_dir.resolve()
if DIR.exists() and (not DIR.is_dir() or any(DIR.iterdir())):
    parser.error('output directory must be new or empty; existing files will not be replaced')
DIR.mkdir(parents=True, exist_ok=True)

# Authors are the fictional identities in the public VLMP example, not people
# inferred from imagery. The graphic scene and detections are synthetic.
manifest = json.loads((EXAMPLES / 'attribution-channel-classifier/model-package.json').read_text(encoding='utf-8'))
data = manifest['extensions']['org.vl-exchange.attribution']['data']
names = [author['name'] for author in data['authors']]
if len(names) != 2:
    raise ValueError('This illustrative layout expects two fictional authors')

W,H,FPS,SECONDS=1280,720,20,16

def choose_font(explicit, candidates):
    if explicit:
        return explicit
    for candidate in candidates:
        try:
            ImageFont.truetype(candidate, 12)
            return candidate
        except OSError:
            pass
    parser.error('No supported font found; provide --font and --bold-font')

FONT = choose_font(args.font, ['/System/Library/Fonts/Supplemental/Arial.ttf',
                    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                    '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf', 'DejaVuSans.ttf'])
BOLD = choose_font(args.bold_font, ['/System/Library/Fonts/Supplemental/Arial Bold.ttf',
                    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                    '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf', 'DejaVuSans-Bold.ttf'])
fsmall=ImageFont.truetype(FONT,17)
fmedium=ImageFont.truetype(FONT,20)
fbold=ImageFont.truetype(BOLD,22)
flabel=ImageFont.truetype(BOLD,15)
fbrand=ImageFont.truetype(BOLD,23)

# Background: deliberately illustrated, not a real customer recording.
back=Image.new('RGB',(W,H)); p=back.load()
for y in range(H):
    for x in range(W):
        glow=max(0, 1-math.hypot((x-900)/1200,(y-280)/650))
        p[x,y]=(int(17+22*glow),int(30+30*glow),int(36+32*glow))
d=ImageDraw.Draw(back,'RGBA')
# Wall and ceiling in desaturated blue-grey.
d.polygon([(0,0),(1280,0),(1280,390),(0,440)], fill=(29,45,50,230))
d.polygon([(0,0),(1280,0),(1280,130),(0,165)], fill=(18,29,35,255))
for x in range(-120,1450,275):
    d.polygon([(x,0),(x+35,0),(x+200,162),(x+165,162)], fill=(15,25,30,200))
for x in [180,500,820,1140]:
    d.rounded_rectangle((x,84,x+125,91),3,fill=(170,207,209,145))
# Rear wall panels.
for x in range(20,1300,157):
    d.rectangle((x,165,x+4,423), fill=(12,25,29,105))
    d.line((x+4,196,x+148,196),fill=(145,172,164,36),width=1)
d.polygon([(0,439),(1280,389),(1280,720),(0,720)],fill=(62,77,74,255))
# Perspective floor seams and subtle safety markings.
for x in range(-1000,2300,150):
    d.line([(650,390),(x,720)], fill=(178,191,176,34),width=2)
for yy in [455,510,585,675]:
    d.line([(0,yy+26),(1280,yy-20)], fill=(194,205,185,30),width=2)
d.polygon([(30,650),(390,639),(610,437),(568,439)],fill=(214,157,58,36))
d.line([(25,648),(390,637),(606,438)],fill=(220,176,71,115),width=4)
# Industrial roller doors and side shelving.
d.rounded_rectangle((39,208,339,420),4,fill=(24,36,41,255),outline=(121,146,143,90),width=3)
for yy in range(225,409,19):d.line((48,yy,330,yy),fill=(146,164,154,42),width=2)
d.rectangle((868,202,1205,435),fill=(20,31,36,245),outline=(122,151,142,90),width=3)
for xx in [883,991,1098,1192]:d.rectangle((xx,206,xx+5,436),fill=(143,163,146,105))
for yy in [252,317,382]:d.rectangle((875,yy,1201,yy+7),fill=(125,146,129,100))
for x,y in [(898,216),(936,218),(1011,214),(1109,217),(1152,220),(900,280),(956,282),(1010,278),(1120,281),(1158,281),(899,342),(950,342),(1022,342),(1108,344)]:
    d.rectangle((x,y,x+30,y+26),fill=(136,113,79,170),outline=(192,162,116,80),width=1)
    d.line((x+14,y,x+14,y+26),fill=(49,43,36,110),width=1)
# Fixed environmental details, a small forklift in the distance.
d.rounded_rectangle((425,343,545,420),8,fill=(56,75,69,255),outline=(170,158,89,130),width=3)
d.rectangle((455,312,525,350),fill=(50,66,63,255),outline=(165,176,149,110),width=3)
d.ellipse((430,403,460,433),fill=(13,24,25,255));d.ellipse((514,403,544,433),fill=(13,24,25,255))
d.line((541,319,541,427,592,427),fill=(180,161,96,130),width=5)
# Camera vignette for a subtle CCTV feel.
vig=Image.new('L',(W,H));vp=vig.load()
for y in range(H):
 for x in range(W):
  v=max(0,min(130,int((math.hypot((x-W/2)/(W/2),(y-H/2)/(H/2))-0.42)*85)))
  vp[x,y]=v
back=Image.composite(Image.new('RGB',(W,H),(5,12,15)),back,vig)

def worker(draw,x,y,scale,phase):
    """Abstract worker, intentionally illustrated, standing at foot point x,y."""
    s=scale
    def q(v):return int(v*s)
    # soft floor shadow
    draw.ellipse((x-q(35),y-q(8),x+q(42),y+q(8)),fill=(4,14,17,100))
    swing=math.sin(phase)*q(8)
    draw.line([(x-q(12),y-q(58)),(x-q(14)+swing,y-q(8))],fill=(25,34,35,255),width=q(13))
    draw.line([(x+q(13),y-q(58)),(x+q(15)-swing,y-q(8))],fill=(25,34,35,255),width=q(13))
    draw.line([(x-q(14)+swing,y-q(5)),(x-q(1)+swing,y-q(5))],fill=(11,19,20,255),width=q(9))
    draw.line([(x+q(15)-swing,y-q(5)),(x+q(28)-swing,y-q(5))],fill=(11,19,20,255),width=q(9))
    draw.polygon([(x-q(23),y-q(130)),(x+q(23),y-q(130)),(x+q(28),y-q(56)),(x-q(25),y-q(56))],fill=(220,159,53,255))
    draw.polygon([(x-q(16),y-q(125)),(x+q(16),y-q(125)),(x+q(16),y-q(65)),(x-q(16),y-q(65))],fill=(223,166,58,255))
    draw.line([(x-q(18),y-q(98)),(x+q(20),y-q(98))],fill=(227,228,189,210),width=q(7))
    draw.line([(x-q(24),y-q(121)),(x-q(39),y-q(76))],fill=(54,70,66,255),width=q(12))
    draw.line([(x+q(24),y-q(121)),(x+q(39),y-q(76))],fill=(54,70,66,255),width=q(12))
    draw.ellipse((x-q(16),y-q(163),x+q(16),y-q(129)),fill=(164,132,103,255))
    draw.pieslice((x-q(19),y-q(173),x+q(19),y-q(140)),180,360,fill=(233,187,76,255))
    draw.rectangle((x-q(22),y-q(157),x+q(22),y-q(153)),fill=(238,194,86,255))
    return (x-q(48),y-q(180),x+q(48),y+q(12))

def panel(draw,rect,fill=(12,24,30,215),outline=(190,226,213,45),radius=12):
    draw.rounded_rectangle(rect,radius=radius,fill=fill,outline=outline,width=1)

def box(draw,rect):
    x0,y0,x1,y1=rect; c=(92,241,188,230); n=18
    # thin border and bright corner markers
    draw.rectangle(rect,outline=(92,241,188,145),width=2)
    for x,dx in [(x0,1),(x1,-1)]:
      for y,dy in [(y0,1),(y1,-1)]:
        draw.line((x,y,x+dx*n,y),fill=c,width=4);draw.line((x,y,x,y+dy*n),fill=c,width=4)
    draw.rounded_rectangle((x0,y0-29,x0+73,y0-3),radius=4,fill=(74,221,166,230))
    draw.text((x0+8,y0-26),'PERSON',font=flabel,fill=(10,36,30,255))

def overlay(img, rects, seconds):
    overlay=Image.new('RGBA',(W,H));o=ImageDraw.Draw(overlay,'RGBA')
    for r in rects:box(o,r)
    panel(o,(20,20,380,67))
    o.ellipse((37,38,47,48),fill=(97,232,174,255))
    o.text((59,31),'CAM 04  ·  SIMULATED VIDEO',font=fbold,fill=(236,245,242,255))
    panel(o,(943,20,1260,67))
    o.text((960,32),'ILLUSTRATIVE  ·  NO REAL AI',font=fsmall,fill=(203,223,218,255))
    # Screen-space brand area, below and away from the simulated detections.
    # Metadata's preferred text provides both names; timing demonstrates both layouts.
    if seconds < 8:
      panel(o,(22,623,340,698),fill=(10,22,28,235),radius=9)
      o.rectangle((22,623,28,698),fill=(99,231,174,255))
      o.text((43,632),'ANALYTICS BY',font=flabel,fill=(171,195,189,255))
      o.text((43,656),names[0],font=fbrand,fill=(245,251,249,255))
    else:
      panel(o,(22,623,477,698),fill=(10,22,28,235),radius=9)
      o.rectangle((22,623,28,698),fill=(99,231,174,255))
      o.text((43,632),'ANALYTICS BY',font=flabel,fill=(171,195,189,255))
      o.text((43,656),names[0]+'  ·  '+names[1],font=fbrand,fill=(245,251,249,255))
    panel(o,(1065,648,1258,697),fill=(10,22,28,220),radius=9)
    o.text((1082,662),('LIVE VIEW' if seconds<8 else 'EXPORT COPY'),font=flabel,fill=(215,232,226,255))
    return Image.alpha_composite(img.convert('RGBA'),overlay).convert('RGB')

def encoder(out):
 return subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-n','-f','rawvideo','-vcodec','rawvideo','-pix_fmt','rgb24','-s',f'{W}x{H}','-r',str(FPS),'-i','-','-an','-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-movflags','+faststart',str(out)],stdin=subprocess.PIPE)

source=encoder(DIR/'01-synthetic-original.mp4')
branded=encoder(DIR/'02-branded-demo.mp4')
try:
 for i in range(FPS*SECONDS):
    t=i/FPS
    img=back.copy().convert('RGBA');draw=ImageDraw.Draw(img,'RGBA')
    xa=int(270+t*22);xb=int(1015-t*15)
    # Both figures stay clear of the lower-third branding and status elements.
    r1=worker(draw,xa,569,1.00,t*4.8)
    r2=worker(draw,xb,510,0.83,t*3.8)
    orig=img.convert('RGB');show=overlay(orig,(r1,r2),t)
    source.stdin.write(orig.tobytes()); branded.stdin.write(show.tobytes())
    if i in (2*FPS,10*FPS):show.save(DIR/f'preview-{int(t):02}.png')
finally:
 source.stdin.close();branded.stdin.close()
 sc=source.wait();bc=branded.wait()
 if sc or bc: raise RuntimeError(f'ffmpeg failed: source={sc}, branded={bc}')
print(f'Complete: {SECONDS}s, {FPS} fps, {W}x{H}; authors: {names}')
