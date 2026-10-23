import {createHash} from 'node:crypto';
import {mkdirSync,readFileSync,rmSync,writeFileSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
export function contentTag(bytes){
 const digest=createHash('sha256').update(bytes).digest().subarray(0,5);
 let n=0n;for(const b of digest)n=(n<<8n)+BigInt(b);
 let s='';for(let i=0;i<8;i++){s=alphabet[Number(n&31n)]+s;n>>=5n;}
 return s;
}
const paintSrc=readFileSync(resolve(root,'terminal/src/track-paint.js'));
const paintTag=contentTag(paintSrc);
const intentSrc=readFileSync(resolve(root,'terminal/src/track-intent.js'));
const intentTag=contentTag(intentSrc);
const rel=name=>`from ${"'"}${name}${"'"}`;
const chartSpec='.'+'/chart.js',paintSpec='.'+'/track-paint.js',intentSpec='.'+'/track-intent.js';
const workspaceSrc=readFileSync(resolve(root,'terminal/src/track-workspace.js'),'utf8').replace(rel(chartSpec),rel('/terminal/src/chart.js')).replace(rel(paintSpec),rel(`./track-paint-${paintTag}.js`)).replace(rel(intentSpec),rel(`./track-intent-${intentTag}.js`));
const workspaceTag=contentTag(workspaceSrc);
const out=resolve(root,'terminal/dist/_app');
rmSync(out,{recursive:true,force:true});
mkdirSync(out,{recursive:true});
const paintName=`track-paint-${paintTag}.js`;
const intentName=`track-intent-${intentTag}.js`;
const workspaceName=`track-workspace-${workspaceTag}.js`;
writeFileSync(resolve(out,paintName),paintSrc);
writeFileSync(resolve(out,intentName),intentSrc);
writeFileSync(resolve(out,workspaceName),workspaceSrc);
writeFileSync(resolve(out,'track-style.js'),readFileSync(resolve(root,'terminal/src/track-style.js')));
let previous={};
try{previous=JSON.parse(readFileSync(resolve(root,'terminal/track-build.json'),'utf8'));}catch{}
const manifest={
 schema:'ticking.track-build.v1',
 paint:`/terminal/dist/_app/${paintName}`,
 intent:`/terminal/dist/_app/${intentName}`,
 workspace:`/terminal/dist/_app/${workspaceName}`,
 sha256:{
  paint:createHash('sha256').update(paintSrc).digest('hex'),
  intent:createHash('sha256').update(intentSrc).digest('hex'),
  workspace:createHash('sha256').update(workspaceSrc).digest('hex'),
 },
 ...(previous.production?{production:previous.production}:{}),
};
writeFileSync(resolve(root,'terminal/track-build.json'),JSON.stringify(manifest,null,2)+'\n');
console.log(JSON.stringify(manifest));
