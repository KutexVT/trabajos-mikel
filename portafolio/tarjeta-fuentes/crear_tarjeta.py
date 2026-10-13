from pathlib import Path

from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, Color, white
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.barcode import qrencoder

OUT = Path(__file__).resolve().parent
TMP = OUT / 'tmp'
TMP.mkdir(exist_ok=True)
URL = 'https://mikelvillegas.netlify.app/#proyectos'
pdfmetrics.registerFont(TTFont('Sans', '/usr/share/fonts/TTF/DejaVuSans.ttf'))
pdfmetrics.registerFont(TTFont('Bold', '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'))
BLUE = HexColor('#4d9fff')
SOFT = HexColor('#c8c5d1')
LAVENDER = HexColor('#c2c1ff')


def text(c, x, y, value, size=7, color=white, font='Sans'):
    c.setFillColor(color)
    c.setFont(font, size)
    c.drawString(x * mm, y * mm, value)


def panel(c, x, y, w, h, radius=2, alpha=.82):
    c.saveState()
    c.setFillColor(Color(13/255, 18/255, 32/255, alpha=alpha))
    c.setStrokeColor(Color(77/255, 159/255, 1, alpha=.24))
    c.setLineWidth(.25)
    c.roundRect(x*mm, y*mm, w*mm, h*mm, radius*mm, fill=1, stroke=1)
    c.restoreState()


def draw_card(c, bleed=0):
    c.saveState()
    c.translate(bleed*mm, bleed*mm)
    c.saveState()
    p = c.beginPath()
    p.rect(-bleed*mm, -bleed*mm, (90+2*bleed)*mm, (50+2*bleed)*mm)
    c.clipPath(p, stroke=0)
    c.linearGradient(0, 50*mm, 90*mm, 0,
                     [HexColor('#244c70'), HexColor('#0d1220'), HexColor('#3c2465')],
                     positions=[0, .48, 1])
    c.restoreState()
    panel(c, 2.6, 2.6, 84.8, 44.8, 2.4)
    # A fine reflection along the top edge carries the website's glass finish
    c.setStrokeColor(Color(1, 1, 1, alpha=.08))
    c.setLineWidth(.2)
    c.line(5*mm, 47.25*mm, 85*mm, 47.25*mm)

    text(c, 6.5, 41.7, 'MV', 11, font='Bold')
    text(c, 12.8, 41.7, '_', 11, BLUE, 'Bold')
    text(c, 18, 42.4, 'DESARROLLO WEB', 5.7, LAVENDER, 'Bold')
    text(c, 18, 39.2, 'FREELANCE / COSTA RICA', 4.6, SOFT)

    text(c, 6.5, 31.7, 'Mikel Villegas', 13.1, font='Bold')
    text(c, 6.5, 26.9, 'FULL STACK DEVELOPER', 6.8, BLUE, 'Bold')
    text(c, 6.5, 22.9, 'Páginas y sistemas web', 6.5, SOFT)

    text(c, 6.5, 15.3, 'kutex.contacto@gmail.com', 6.2, SOFT)
    text(c, 6.5, 11.0, '+506 6100 3542', 7.2, white)
    c.linkURL('mailto:kutex.contacto@gmail.com', (6.5*mm, 14.6*mm, 52*mm, 18*mm), relative=1)
    c.linkURL('tel:+50661003542', (6.5*mm, 10*mm, 43*mm, 14*mm), relative=1)

    panel(c, 57.2, 9.9, 27.6, 33.8, 2)
    code = qrencoder.QRCode(version=None, errorCorrectLevel=qrencoder.QRErrorCorrectLevel.M)
    code.addData(URL)
    code.make()
    modules = code.getModuleCount()
    size = 24.4*mm
    x, y = 58.8*mm, 17.6*mm
    unit = size/(modules+8)
    c.setFillColor(white)
    c.roundRect(x, y, size, size, .8*mm, fill=1, stroke=0)
    c.setFillColor(HexColor('#0d1220'))
    for row in range(modules):
        for col in range(modules):
            if code.isDark(row, col):
                c.rect(x+(col+4)*unit, y+(modules+3-row)*unit, unit, unit, fill=1, stroke=0)
    c.setFont('Bold', 5.5)
    c.setFillColor(BLUE)
    c.drawCentredString(71*mm, 13*mm, 'VER PROYECTOS')
    c.linkURL(URL, (57.2*mm, 9.9*mm, 84.8*mm, 43.7*mm), relative=1)

    c.setStrokeColor(Color(77/255, 159/255, 1, alpha=.18))
    c.line(6.5*mm, 8*mm, 83.5*mm, 8*mm)
    text(c, 6.5, 5.2, 'mikelvillegas.netlify.app', 5.6, LAVENDER)
    c.linkURL(URL, (6.5*mm, 4.5*mm, 51*mm, 7.5*mm), relative=1)
    c.restoreState()


def export(path, bleed):
    c = canvas.Canvas(str(path), pagesize=((90+bleed*2)*mm, (50+bleed*2)*mm))
    c.setTitle('Mikel Villegas | Tarjeta de presentación')
    c.setAuthor('Mikel Villegas')
    c.setSubject('90 × 50 mm · 3 mm de sangrado · QR al portafolio' if bleed else 'Tarjeta de presentación')
    if bleed:
        c.setTrimBox((bleed*mm, bleed*mm, (90+bleed)*mm, (50+bleed)*mm))
        c.setBleedBox((0, 0, (90+2*bleed)*mm, (50+2*bleed)*mm))
    draw_card(c, bleed)
    c.showPage()
    c.save()


export(OUT / 'tarjeta-mikel-imprimir.pdf', 3)
export(TMP / 'tarjeta-mikel.pdf', 0)
print(URL)
