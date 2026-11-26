const installReportReader=(doc)=>{
  const article=doc.querySelector('body > article.report');
  if(!article || doc.querySelector('body > nav.reader-toc')) return;
  const body=doc.body;
  const narrow=window.matchMedia('(max-width: 1279px)');
  const headings=[...article.querySelectorAll('h2, h3')].filter(
    (heading)=>!heading.closest('.summary, details, figure'));
  const titles=headings.map((heading)=>(heading.textContent || '').trim());

  const notes=[];
  let noteNumber=0;
  for(const note of article.querySelectorAll('aside')){
    noteNumber+=1;
    const anchor=note.previousElementSibling;
    if(!anchor || anchor.tagName==='ASIDE') continue;
    const ref=doc.createElement('sup');
    ref.className='note-ref';
    ref.setAttribute('aria-hidden','true');
    ref.setAttribute('data-rcp-selection','');
    ref.textContent=String(noteNumber);
    anchor.append(ref);
    notes.push([note,anchor]);
  }
  const placeNotes=()=>{
    for(const [note,anchor] of notes){
      if(narrow.matches) anchor.after(note);
      else anchor.before(note);
    }
  };
  placeNotes();
  narrow.addEventListener('change',placeNotes);

  if(headings.length<2) return;
  const nav=doc.createElement('nav');
  nav.className='reader-toc';
  nav.setAttribute('aria-label','Contents');
  nav.setAttribute('data-rcp-selection','');
  const toggle=doc.createElement('button');
  toggle.type='button';
  toggle.className='reader-toc-toggle';
  toggle.innerHTML='<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg><span class="reader-toc-label">Contents</span>';
  const flag=()=>narrow.matches?'reader-toc-open':'reader-toc-closed';
  const sync=()=>{
    const open=narrow.matches
      ? body.classList.contains('reader-toc-open')
      : !body.classList.contains('reader-toc-closed');
    toggle.setAttribute('aria-expanded',String(open));
  };
  toggle.addEventListener('click',()=>{ body.classList.toggle(flag()); sync(); });
  narrow.addEventListener('change',sync);
  const list=doc.createElement('ol');
  const entries=new Map();
  headings.forEach((heading,index)=>{
    const item=doc.createElement('li');
    if(heading.tagName==='H3') item.className='sub';
    const entry=doc.createElement('button');
    entry.type='button';
    entry.className='reader-toc-entry';
    entry.textContent=titles[index];
    entry.addEventListener('click',()=>{
      heading.scrollIntoView({behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});
      if(narrow.matches){ body.classList.remove('reader-toc-open'); sync(); }
    });
    item.append(entry);
    list.append(item);
    entries.set(heading,entry);
  });
  nav.append(toggle,list);
  article.before(nav);
  sync();

  let current=null;
  const mark=()=>{
    let active=headings[0];
    for(const heading of headings){
      if(heading.getBoundingClientRect().top<=window.innerHeight*0.25) active=heading;
      else break;
    }
    if(active===current) return;
    entries.get(current)?.removeAttribute('aria-current');
    entries.get(active)?.setAttribute('aria-current','true');
    current=active;
  };
  let pending=false;
  const schedule=()=>{
    if(pending) return;
    pending=true;
    window.requestAnimationFrame(()=>{ pending=false; mark(); });
  };
  window.addEventListener('scroll',schedule,{passive:true});
  window.addEventListener('resize',schedule);
  schedule();
};
if(document.readyState==='loading')
  document.addEventListener('DOMContentLoaded',()=>installReportReader(document),{once:true});
else installReportReader(document);
