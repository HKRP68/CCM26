"""Downloadable PNG record cards, rendered from the current verified board."""
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_DIR=Path(__file__).resolve().parents[1]/'assets'/'fonts'


def render(series_name,scope,board):
    image=Image.new('RGB',(1200,675),'#10263c');draw=ImageDraw.Draw(image)
    def font(size):
        try:return ImageFont.truetype(str(FONT_DIR/'BricolageGrotesque-Regular.ttf'),size)
        except OSError:return ImageFont.load_default(size=size)
    def write(text,xy,size,color='#ffffff',max_width=1060):
        while size>16 and draw.textlength(text,font=font(size))>max_width:size-=1
        draw.text(xy,text,font=font(size),fill=color)
    draw.rectangle((0,0,1200,13),fill='#f9d160')
    write(series_name,(65,50),38)
    write(scope.upper(),(65,112),23,'#79d6cb')
    write(board['label'],(65,184),42)
    if board['holders']:
        write(f"{board['holders'][0]['display']} {board['unit']}",(65,254),96,'#f9d160')
        holders=list(dict.fromkeys(h['name'] for h in board['holders']))
        write(' / '.join(holders[:3]),(65,398),40)
        if len(holders)>3:write(f'+ {len(holders)-3} joint holders',(65,452),24,'#b6c8d5')
        source=board['holders'][0].get('source')
        if source:
            write(f"{source.get('team1') or ''} v {source.get('team2') or ''} | Match #{source.get('match_id') or source.get('fixture_id')}",(65,510),25,'#b6c8d5')
    else:write('No qualifying performance',(65,280),44,'#b6c8d5')
    draw.line((65,578,1135,578),fill='#446176',width=2)
    write('CMU RECORD BOOK  |  Verified scorecard evidence',(65,602),22,'#b6c8d5')
    stream=BytesIO();image.save(stream,format='PNG');stream.seek(0)
    return stream
