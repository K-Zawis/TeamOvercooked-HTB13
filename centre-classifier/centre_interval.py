"""Compare full legend motifs with native-scale, motif-height track slices."""
import argparse
from functools import partial
from statistics import median
from pathlib import Path
import interval_legend as base
from segmentation import refine_intervals

STRIP_HEIGHT = 32
SAMPLER_VERSION = 'row-boundaries-full-motifs-0.5-v3'

def patches(read,box,row,strip_height=STRIP_HEIGHT):
    x0,y0,x1,y1=box;a,b=row['top_y'],row['bottom_y']
    if not (x0<x1 and y0<=a<b<=y1):raise ValueError('invalid bounds')
    trim=max(1,round((x1-x0)*.08));lo=x0+trim;hi=x1-trim
    if hi<=lo:raise ValueError('track too narrow')
    pad=min(4,(b-a)//8);top=a+pad;bottom=b-pad
    if type(strip_height) is not int or strip_height<1:raise ValueError('invalid strip height')
    # Cover the interval interior with motif-height slices. Align the last
    # slice to the bottom instead of leaving a tiny, unrepresentative tail.
    w=hi-lo;h=min(strip_height,bottom-top);left=lo
    starts=list(range(top,bottom-h+1,h))
    if starts[-1]!=bottom-h:starts.append(bottom-h)
    boxes=[];images=[]
    for y in starts:
        crop=[left,y,left+w,y+h]
        if crop in boxes:continue
        im=read(crop).convert('RGB')
        if im.size!=(w,h):raise ValueError('incorrect reader dimensions')
        images.append(im);boxes.append(crop)
    return images,boxes

def refs(spec,root):
    # The configured box is the complete motif interior, excluding frame/text.
    return base.legend_refs(spec,root)

def prepare(rows,box,read,refs,out,source_identity,mode='native_original'):
    rows=refine_intervals(rows,box,read,refs)
    height=max(1,round(median(r['im'].height for r in refs)))
    previous=base.patches;base.patches=partial(patches,strip_height=height)
    try:return base.prepare(rows,box,read,refs,out,source_identity,mode)
    finally:base.patches=previous

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--image',required=True);ap.add_argument('--annotation',required=True);ap.add_argument('--intervals',required=True);ap.add_argument('--legend',required=True);ap.add_argument('--out',required=True);a=ap.parse_args()
    spec=base.load(a.legend);identity=base.filehash(a.image)
    if spec.get('source_sha256')!=identity:raise ValueError('legend/source mismatch')
    reader=base.Raster(a.image)
    try:prepare(base.load(a.intervals),base.load(a.annotation)['bounding_boxes']['interpreted_lithology_column'],reader.read,refs(spec,Path(a.legend).parent),a.out,identity)
    finally:reader.close()
if __name__=='__main__':main()
