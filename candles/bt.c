
#include <math.h>
#include <stdlib.h>
void run_loop(int n,int start,double*c,double*h,double*l,double*bbu,double*bbm,double*bbl,double*stk,double*std_,double*ema,int has_ema,double bbpL,double bbpS,double stoch_os,double stoch_ob,double tp,double sl,int mid_exit,double*pf,double*wr,int*nt,double*roi,double*aw,double*al,double*tpd,double*mdd,double dias){
double*buf=(double*)malloc(n*sizeof(double));int ntrades=0,in_trade=0,side=0;double ep=0,slp=0,tpp=0;
for(int i=start;i<n;i++){if(in_trade){double bm=bbm[i];if(side==0){if(l[i]<=slp){buf[ntrades++]=(slp-ep)/ep*100;in_trade=0;continue;}if(h[i]>=tpp){buf[ntrades++]=(tpp-ep)/ep*100;in_trade=0;continue;}if(mid_exit&&!isnan(bm)&&c[i]>=bm){buf[ntrades++]=(bm-ep)/ep*100;in_trade=0;continue;}}else{if(h[i]>=slp){buf[ntrades++]=(ep-slp)/ep*100;in_trade=0;continue;}if(l[i]<=tpp){buf[ntrades++]=(ep-tpp)/ep*100;in_trade=0;continue;}if(mid_exit&&!isnan(bm)&&c[i]<=bm){buf[ntrades++]=(ep-bm)/ep*100;in_trade=0;continue;}}continue;}
if(isnan(bbu[i])||isnan(bbm[i])||isnan(bbl[i])||isnan(bbu[i-1])||isnan(bbm[i-1])||isnan(bbl[i-1])||isnan(stk[i])||isnan(std_[i])||isnan(stk[i-1])||isnan(std_[i-1]))continue;
double br=bbu[i-1]-bbl[i-1]+1e-9,bbp=(c[i-1]-bbl[i-1])/br;
int lb=(bbp<bbpL)&&(c[i]>bbl[i])&&(c[i]<bbm[i]);int sb=(bbp>bbpS)&&(c[i]<bbu[i])&&(c[i]>bbm[i]);int ls=(stk[i]<stoch_os)&&(stk[i]>std_[i])&&(stk[i-1]<=std_[i-1]);int ss=(stk[i]>stoch_ob)&&(stk[i]<std_[i])&&(stk[i-1]>=std_[i-1]);
if(has_ema&&!isnan(ema[i])){if(lb&&ls&&c[i]<ema[i])lb=0;if(sb&&ss&&c[i]>ema[i])sb=0;}
if(lb&&ls){in_trade=1;ep=c[i];side=0;slp=ep*(1-sl/100);tpp=ep*(1+tp/100);}else if(sb&&ss){in_trade=1;ep=c[i];side=1;slp=ep*(1+sl/100);tpp=ep*(1-tp/100);}
}
double gp=0,gl=0,r=0,sw=0,sl2=0,eq=0,pk=0,md=0;int nw=0;
for(int i=0;i<ntrades;i++){double v=buf[i];r+=v;eq+=v;if(eq>pk)pk=eq;double dd=pk-eq;if(dd>md)md=dd;if(v>0){gp+=v;nw++;sw+=v;}else{gl+=-v;sl2+=v;}}
*nt=ntrades;*pf=(gl>0)?gp/gl:999.0;*wr=(ntrades>0)?(double)nw/ntrades*100:0;*roi=r;*aw=(nw>0)?sw/nw:0;*al=(ntrades-nw>0)?sl2/(ntrades-nw):0;*tpd=(dias>0)?ntrades/dias:0;*mdd=md;free(buf);}
