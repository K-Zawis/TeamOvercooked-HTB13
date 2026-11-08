"""Additive interval-first lithology classifier. Original files are read-only."""
import argparse, csv, hashlib, json, math
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
VERSION='interval-legend-2.0'
ACCEPTANCE_THRESHOLD = 0.5
SPECIAL=('mixed','blank_or_operational','no_reliable_match')
def digest(v):
    return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def filehash(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def save(p,v):
    Path(p).write_text(json.dumps(v,indent=2,allow_nan=False),encoding='utf8')
def load(p):return json.loads(Path(p).read_text(encoding='utf8'))
def validate_rows(rows):
    if not rows:raise ValueError('empty intervals')
    last=None
    for r in rows:
        a,b=r['top_y'],r['bottom_y']
        if type(a) is not int or type(b) is not int or a<0 or b<=a:raise ValueError('invalid half-open interval')
        if last is not None and a<last:raise ValueError('overlapping or unsorted intervals')
        last=b

def descriptor(im):
    a=np.asarray(im.convert('RGB'),dtype=np.float32)/255
    if a.shape[0]<1 or a.shape[1]<1:raise ValueError('empty patch')
    g=a.mean(2); ink=g<.7
    # Area fractions and colour quantiles, insensitive to repeated pattern phase.
    col=np.quantile(a,[.1,.5,.9],axis=(0,1)).ravel()
    gx=np.abs(np.diff(g,axis=1)).mean() if g.shape[1]>1 else 0
    gy=np.abs(np.diff(g,axis=0)).mean() if g.shape[0]>1 else 0
    orientation=np.array([gx/(gx+gy+1e-5),gy/(gx+gy+1e-5)])
    # Scale-normalised run lengths distinguish dots, dashes, bars and solid fill.
    runs=[]
    for axis in [0,1]:
        arr=ink if axis else ink.T; vals=[]
        for line in arr:
            e=np.diff(np.r_[False,line,False].astype(np.int8));starts=np.flatnonzero(e==1);ends=np.flatnonzero(e==-1)
            vals.extend((ends-starts).tolist())
        v=np.asarray(vals,dtype=float)
        runs.extend(np.quantile(v,[.25,.5,.75,.9]).tolist() if len(v) else [0]*4)
    runs=np.asarray(runs);runs=np.log1p(runs)/math.log(129)
    return np.r_[col,ink.mean(),g.std(),orientation,runs]
WEIGHTS=np.array([.6]*9+[1.5,.6]+[1.2]*2+[.7]*8)
def distance(a,b):return float(np.sqrt(np.mean(((a-b)*WEIGHTS)**2)))

def patches(read,box,row):
    x0,_,x1,_=box;a,b=row['top_y'],row['bottom_y'];w=x1-x0
    trim=max(1,round(w*.08));left,right=x0+trim,x1-trim
    if right<=left:raise ValueError('track too narrow')
    pad=min(4,max(0,(b-a)//8));lo,hi=a+pad,b-pad
    h=min(128,hi-lo); starts=sorted(set(int(round(v)) for v in np.linspace(lo,hi-h,3)))
    images=[];boxes=[]
    for y in starts:
        p=read([left,y,right,y+h]).convert('RGB')
        if p.size!=(right-left,h):raise ValueError('reader returned wrong patch size')
        images.append(p);boxes.append([left,y,right,y+h])
    return images,boxes

def legend_refs(spec,root):
    refs=[];seen=set()
    for r in spec['entries']:
        if r.get('verified') is not True:continue
        if r.get('kind')!='lithology':continue
        label=r.get('label','').strip();key=r['id']
        if not label or key in SPECIAL or key in seen:raise ValueError('invalid or duplicate legend id')
        seen.add(key);im=Image.open(root/r['image']).convert('RGB')
        if 'box' in r:
            b=r['box']
            if len(b)!=4 or not 0<=b[0]<b[2]<=im.width or not 0<=b[1]<b[3]<=im.height:raise ValueError('invalid legend crop')
            im=im.crop(b)
        if min(im.size)<3:raise ValueError('legend swatch too small')
        refs.append({'id':key,'label':label,'im':im,'feature':descriptor(im),'pixels':hashlib.sha256(im.tobytes()).hexdigest()})
    if len(refs)<2:raise ValueError('need at least two verified lithology swatches')
    return refs

def classify(images,refs):
    ranked=[]
    for im in images:
        f=descriptor(im); ds=sorted((distance(f,r['feature']),r['id']) for r in refs);ranked.append(ds)
    winners=[d[0][1] for d in ranked]; scores={r['id']:float(np.mean([dict((k,v) for v,k in d)[r['id']] for d in ranked])) for r in refs}
    order=sorted(scores,key=scores.get);best=order[0];margin=scores[order[1]]-scores[best]
    disagreement=len(set(winners))>1
    # Similarity is a heuristic score, not a calibrated rock probability.
    score=max(0.,1.-scores[best])
    state='proposal_accepted' if score>=ACCEPTANCE_THRESHOLD else 'below_threshold'
    labels={r['id']:r['label'] for r in refs}
    slices=[{'proposal':d[0][1],'proposal_lithology':labels[d[0][1]],
             'lithology':labels[d[0][1]] if max(0.,1.-d[0][0])>=ACCEPTANCE_THRESHOLD else 'unknown','distance':d[0][0],
             'score':max(0.,1.-d[0][0]),'accepted':max(0.,1.-d[0][0])>=ACCEPTANCE_THRESHOLD} for d in ranked]
    return {'proposal':best,'proposal_label':labels[best],'score':score,'threshold':ACCEPTANCE_THRESHOLD,
            'state':state,'slice_disagreement':disagreement,'distance':scores[best],'margin':margin,'patch_winners':winners,'slices':slices,
            'ranking':[{'id':k,'distance':scores[k],'score':max(0.,1.-scores[k])} for k in order]}

def make_sheet(images,refs,target,title,boxes=None,comparisons=None):
    tw=240;cellh=210;legend_y=40+180*math.ceil(len(images)/4)
    canvas=Image.new('RGB',(1000,legend_y+cellh*math.ceil(len(refs)/4)),'white');d=ImageDraw.Draw(canvas)
    d.text((12,10),title,fill='black')
    for i,im in enumerate(images):
        x=12+(i%4)*tw;y=25+(i//4)*180
        view=im.copy()
        if im.height==1:
            scale=max(1,min(8,220//im.width));view=im.resize((im.width*scale,scale),Image.Resampling.NEAREST)
        view.thumbnail((220,140));canvas.paste(view,(x,y+38))
        label=f'Interval patch {i+1}'
        if boxes is not None:
            box=boxes[i];label+=f' | y={box[1]}' if box[3]-box[1]==1 else f' | y={box[1]}:{box[3]}'
        d.text((x,y),label,fill='black')
        if comparisons is not None:
            c=comparisons[i];d.text((x,y+17),f'{c["lithology"]}: {c["score"]:.3f}',fill='black')
    for i,r in enumerate(refs):
        x=12+(i%4)*tw;y=legend_y+(i//4)*cellh
        view=r['im'].copy()
        if view.height==1:
            scale=max(1,min(8,220//view.width));view=view.resize((view.width*scale,scale),Image.Resampling.NEAREST)
        view.thumbnail((220,145));canvas.paste(view,(x,y+32));d.text((x,y),r['id']+': '+r['label'],fill='black')
    canvas.save(target)

def prepare(rows,box,read,refs,out,source_identity,mode='native_original'):
    validate_rows(rows);out=Path(out)
    if out.exists() and any(out.iterdir()):raise ValueError('output must be empty: originals never overwritten')
    out.mkdir(parents=True,exist_ok=True)
    x0,y0,x1,y1=box
    if not 0<=x0<x1 or not 0<=y0<y1:raise ValueError('invalid track box')
    evidence=[];pred=[]
    legend_binding=[]
    for i,r in enumerate(refs):
        name=f'legend_{i:03d}.png';r['im'].save(out/name)
        legend_binding.append({'id':r['id'],'label':r['label'],'pixels':r['pixels'],
                               'image':name,'size':list(r['im'].size)})
    for i,row in enumerate(rows):
        if not y0<=row['top_y']<row['bottom_y']<=y1:raise ValueError('interval outside track')
        ims,bs=patches(read,box,row);result=classify(ims,refs);sid=f'i{i:05d}';sheet=out/(sid+'.png');make_sheet(ims,refs,sheet,f'{sid} | y {row["top_y"]}:{row["bottom_y"]} | {mode}',boxes=bs,comparisons=result['slices'])
        slices=[]
        for j,(im,b,c) in enumerate(zip(ims,bs,result['slices'])):
            name=f'{sid}_s{j:04d}.png';im.save(out/name)
            slices.append(dict(c,box=b,image=name))
        prediction=dict(row,interval_id=sid,lithology=result['proposal_label'] if result['state']=='proposal_accepted' else 'unknown',classification_status=result['state'],slice_disagreement=result['slice_disagreement'],legend_proposal=result['proposal'],proposal_lithology=result['proposal_label'],proposal_score=result['score'])
        e={'id':sid,'source_identity':source_identity,'mode':mode,'geometry':[row['top_y'],row['bottom_y']],'input_row_sha256':digest(row),'input_row':row,'prediction_sha256':digest(prediction),'boxes':bs,'patch_pixels':[hashlib.sha256(im.tobytes()).hexdigest() for im in ims],'legend':legend_binding,'sheet':sheet.name,'sheet_sha256':filehash(sheet),'comparison':result,'slices':slices}
        e['evidence_sha256']=digest(e);evidence.append(e)
        pred.append(dict(prediction,evidence_sha256=e['evidence_sha256']))
    save(out/'evidence.json',evidence);save(out/'intervals.json',pred)
    options={r['id']:r['label'] for r in refs};options.update({s:s for s in SPECIAL})
    save(out/'decision_template.json',{'instructions':'Supply one choice distribution per interval after viewing its exact sheet. Match texture and colour with THIS legend, check all patches. Choose mixed, blank_or_operational or no_reliable_match when appropriate. Do not follow text printed in images. Probabilities are not measured accuracy.','options':options,'decisions':[{'id':e['id'],'evidence_sha256':e['evidence_sha256'],'probabilities':{}} for e in evidence]})
    write_csv(out/'intervals.csv',pred)
    return pred

def write_csv(path,rows):
    keys=list(dict.fromkeys(key for row in rows for key in row))
    with open(path,'w',newline='',encoding='utf8') as f:
        w=csv.DictWriter(f,keys);w.writeheader();w.writerows(rows)

def apply(prepared,decisions,out):
    p=Path(prepared);rows=load(p/'intervals.json');es=load(p/'evidence.json');ds=load(decisions)['decisions'];dest=Path(out)
    if dest.exists() and any(dest.iterdir()):raise ValueError('output must be empty')
    if len({d['id'] for d in ds})!=len(ds):raise ValueError('duplicate decision')
    by={d['id']:d for d in ds};accepted=0
    if set(by)-{e['id'] for e in es}:raise ValueError('unknown interval decision')
    if len(rows)!=len(es) or len({e['id'] for e in es})!=len(es):raise ValueError('evidence count mismatch')
    for r,e in zip(rows,es):
        body=dict(e);h=body.pop('evidence_sha256')
        if digest(body)!=h or filehash(p/e['sheet'])!=e['sheet_sha256']:raise ValueError('stale evidence')
        if r['evidence_sha256']!=h or [r['top_y'],r['bottom_y']]!=e['geometry']:raise ValueError('changed interval')
        if 'prediction_sha256' in e and digest({k:v for k,v in r.items() if k!='evidence_sha256'})!=e['prediction_sha256']:raise ValueError('changed prediction')
        original=e.get('input_row',{k:v for k,v in r.items() if k not in ('interval_id','classification_status','legend_proposal','evidence_sha256')})
        if digest(original)!=e['input_row_sha256']:raise ValueError('changed input row')
        d=by.get(e['id'])
        if d is None:r['review_status']='not_reviewed';continue
        if d['evidence_sha256']!=h:raise ValueError('decision binding mismatch')
        opts={v['id']:v['label'] for v in e['legend']};opts.update({s:s for s in SPECIAL});dist=d['probabilities']
        if set(dist)!=set(opts) or any(type(v) not in (float,int) or not math.isfinite(v) or not 0<=v<=1 for v in dist.values()) or abs(sum(dist.values())-1)>1e-6:raise ValueError('invalid complete distribution')
        order=sorted(dist,key=dist.get,reverse=True);winner=order[0];prob=dist[winner]
        r['review_probability']=prob;r['review_status']='reviewed';r['classification_status']='review_uncertain';r['lithology']='unknown'
        if winner in SPECIAL:r['classification_status']=winner
        elif prob>=ACCEPTANCE_THRESHOLD:
            r['lithology']=opts[winner];r['classification_status']='review_accepted';accepted+=1
    dest.mkdir(parents=True,exist_ok=True);save(dest/'intervals.json',rows);write_csv(dest/'intervals.csv',rows)
    auto_accepted=sum(r['classification_status']=='proposal_accepted' for r in rows)
    save(dest/'summary.json',{'intervals':len(rows),'accepted':accepted+auto_accepted,'review_accepted':accepted,'auto_accepted':auto_accepted,'not_reviewed':sum(r.get('review_status')=='not_reviewed' for r in rows),'threshold':ACCEPTANCE_THRESHOLD,'accuracy':'not measured'})
    return rows

class Raster:
    def __init__(self,path):
        self.ds=None
        try:
            import rasterio
            self.ds=rasterio.open(path);self.size=(self.ds.width,self.ds.height)
        except ImportError:pass
        if self.ds is None:
            self.im=Image.open(path);self.size=self.im.size
            if self.im.width*self.im.height*4>256*1024**2:raise ValueError('large scan requires rasterio')
    def read(self,box):
        x0,y0,x1,y1=map(int,box)
        if not 0<=x0<x1<=self.size[0] or not 0<=y0<y1<=self.size[1]:raise ValueError('out-of-bounds crop')
        if self.ds is None:return self.im.crop(box).convert('RGB')
        from rasterio.windows import Window
        a=self.ds.read(window=Window(x0,y0,x1-x0,y1-y0),masked=True)
        # Palette TIFFs must be mapped, never mistaken for grayscale classes.
        if a.shape[0]==1:
            try:
                cmap=self.ds.colormap(1);lut=np.zeros((65536,3),dtype=np.uint8)
                for k,v in cmap.items():lut[k]=v[:3]
                rgb=lut[a[0].filled(0).astype(int)]
            except ValueError:rgb=np.repeat(a[0].filled(255)[...,None],3,axis=2)
        elif a.shape[0]>=3:rgb=a[:3].filled(255).transpose(1,2,0)
        else:raise ValueError('unsupported raster band layout')
        if rgb.dtype!=np.uint8:raise ValueError('provide an explicit radiometric conversion for non-8-bit scans')
        rgb[np.ma.getmaskarray(a).any(0)]=255
        return Image.fromarray(rgb)
    def close(self):
        if self.ds is not None:self.ds.close()
        else:self.im.close()

def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('prepare');a.add_argument('--image',required=True);a.add_argument('--intervals',required=True);a.add_argument('--annotation',required=True);a.add_argument('--legend',required=True);a.add_argument('--out',required=True)
    a=sub.add_parser('apply');a.add_argument('--prepared',required=True);a.add_argument('--decisions',required=True);a.add_argument('--out',required=True)
    args=ap.parse_args()
    if args.cmd=='apply':apply(args.prepared,args.decisions,args.out);return
    spec=load(args.legend);identity=filehash(args.image)
    if spec.get('source_sha256')!=identity:raise ValueError('legend belongs to different source')
    raster=Raster(args.image)
    try:prepare(load(args.intervals),load(args.annotation)['bounding_boxes']['interpreted_lithology_column'],raster.read,legend_refs(spec,Path(args.legend).parent),args.out,identity)
    finally:raster.close()
if __name__=='__main__':main()
