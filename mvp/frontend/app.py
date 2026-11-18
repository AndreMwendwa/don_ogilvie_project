"""Don Ogilvie Studio — Streamlit UI for the existing physics Manim pipeline."""
from __future__ import annotations
import os
from pathlib import Path
import streamlit as st
from job_engine import create_job, list_jobs, read_job, job_dir, tail_log, videos_for, MAX_UPLOAD_MB, SCRIPT_ROOT, RENDERER

st.set_page_config(page_title='TnK Orbitals | Video Studio', page_icon='🎓', layout='wide', initial_sidebar_state='expanded')

st.markdown('''<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap');
:root {
    --band-height: 218px;
    --sidebar-width: 21rem;
    --primary: #5149c9;
    --workspace: #f7f8fc;
    --shared-band: linear-gradient(110deg, #5147c9 0%, #665bd7 55%, #9184ec 100%);
}
html, body, [class*="css"], [data-testid="stApp"] {font-family:'DM Sans',sans-serif;}
/* IMPORTANT: both surfaces use exactly the same viewport-anchored background.
   The white/light areas start at the same Y coordinate, leaving a seamless band. */
[data-testid="stAppViewContainer"],
[data-testid="stSidebar"] {
    background-image:
      linear-gradient(to bottom, transparent 0, transparent var(--band-height),
         var(--below-band) var(--band-height), var(--below-band) 100%),
      var(--shared-band) !important;
    background-attachment: fixed !important;
    background-position: top left !important;
    background-size: 100vw 100vh !important;
    background-repeat: no-repeat !important;
}
[data-testid="stAppViewContainer"] {--below-band: var(--workspace);}
[data-testid="stSidebar"] {--below-band: #ffffff; border-right:1px solid #e8ebf4;}
[data-testid="stSidebar"] > div, [data-testid="stSidebarContent"] {background: transparent !important;}
[data-testid="stHeader"] {background: transparent !important;}
[data-testid="stDecoration"] {display:none;}
.block-container {padding-top:1.85rem;max-width:1320px;padding-bottom:4rem;}
/* Reserve the same vertical region for branding and page hero. */
.sidebar-brand {min-height:177px;padding:19px 4px 0;box-sizing:border-box;}
.brand-title {color:#fff!important;font-size:24px;font-weight:800;letter-spacing:-.03em;white-space:nowrap;}
.brand-subtitle {color:#f0edff!important;font-size:11px;font-weight:700;letter-spacing:.095em;margin-top:12px;}
.hero {background:transparent!important;box-shadow:none!important;border-radius:0;padding:7px 0 0;min-height:180px;box-sizing:border-box;margin:0 0 17px;color:#fff!important;}
.hero .eyebrow {color:#efecff;font-size:12px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;}
.hero h2 {color:#fff!important;margin:11px 0 0!important;font-size:clamp(23px,2.25vw,34px);line-height:1.18;letter-spacing:-.035em;font-weight:800;}
.hero p {color:#f2f0ff!important;margin:12px 0 0;max-width:890px;font-size:15px;}
/* Sidebar navigation is below the band, on white. */
[data-testid="stSidebar"] [data-testid="stRadio"] label {font-weight:600;}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] {gap:.32rem;}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label {border-radius:11px;padding:8px 9px;}
[data-testid="stSidebar"] [data-testid="stRadio"] [role="radiogroup"] label:has(input:checked) {background:#eeedff;}
h1,h2,h3 {letter-spacing:-.035em; color:#1a2140;}
h1 {font-weight:800!important;}
[data-testid="stMetric"] {background:#fff;border:1px solid #e9edf5;border-radius:17px;padding:17px 19px;box-shadow:0 5px 14px rgba(35,39,89,.05);}
[data-testid="stMetricLabel"] {color:#59637d;}
div.stButton > button[kind="primary"] {background:var(--primary);border-color:var(--primary);color:white;border-radius:11px;font-weight:700;}
div.stButton > button {border-radius:11px;font-weight:600;}
[data-testid="stFileUploader"] {background:#fff;border-radius:16px;padding:12px;border:1px solid #e7eaf4;}
[data-testid="stVerticalBlockBorderWrapper"] {border-color:#e9edf5!important;border-radius:16px!important;background:#fff;}
.pill {display:inline-block;color:#514cc7;background:#eeedff;border-radius:30px;padding:5px 11px;font-size:12px;font-weight:700;}
.muted {color:#7a8297;font-size:14px;}
.step-tag {color:#5856d6;font-weight:800;}
.toplabel {font-size:12px;font-weight:800;color:#727c94;text-transform:uppercase;letter-spacing:.11em;}
.feature-card {background:#fff;border:1px solid #e9edf5;border-radius:17px;padding:22px 23px;min-height:164px;box-shadow:0 5px 14px rgba(35,39,89,.05);}
.feature-card h3 {font-size:19px;margin:8px 0;color:#1a2140;}
.feature-card p {font-size:14px;line-height:1.6;color:#65708b;}
/* On small screens the sidebar collapses; keep hero legible and reduce band height. */
@media (max-width: 760px) {
  :root {--band-height: 250px;}
  .block-container {padding-top:1.6rem;}
  .hero {min-height:212px;}
  .hero h2 {font-size:24px;}
  .hero p {font-size:13px;}
  .sidebar-brand {min-height:205px;}
}
</style>''', unsafe_allow_html=True)

with st.sidebar:
    st.markdown('''<div class="sidebar-brand">
        <div class="brand-title">🎓 TnK Orbitals</div>
        <div class="brand-subtitle">EDUCATIONAL VIDEO STUDIO</div>
    </div>''', unsafe_allow_html=True)
    pages = ['Dashboard', 'Create Video', 'My Jobs', 'Video Library', 'Settings']
    if 'page' not in st.session_state:
        st.session_state.page = 'Dashboard'
    page = st.radio('Workspace', pages, index=pages.index(st.session_state.page), label_visibility='collapsed')
    st.session_state.page = page
    st.divider()
    st.caption('WORKSPACE')
    st.info('Physics PDF pipeline enabled. Image, camera, math and chemistry inputs are on the roadmap.', icon='ℹ️')
    st.caption('Single-host prototype · Private deployment only')

jobs = list_jobs()
completed = [j for j in jobs if j.get('status') == 'completed']
active = [j for j in jobs if j.get('status') in ('running', 'queued')]


def headline(title, subtitle):
    # Shared band is not a hero card: it is one background behind sidebar + main.
    # Predefined strings only (not user-uploaded content) are interpolated here.
    st.markdown(f'''<div class="hero"><div class="eyebrow">TnK Orbitals · Video Studio</div>
        <h2>{title}</h2><p>{subtitle}</p></div>''', unsafe_allow_html=True)


def status_label(status):
    return {'completed':'✅ Completed','running':'🔵 Running','queued':'🟡 Queued','failed':'🔴 Failed'}.get(status,status)


def job_view(j):
    jid = j['id']
    with st.container(border=True):
        col1,col2 = st.columns([5,2])
        with col1:
            st.markdown(f"**📄 {j.get('filename','Workbook')}**")
            st.caption(f"Job {jid[:10]} · Parts {', '.join(map(str,j.get('parts',[])))} · {j.get('created_at','')[:16].replace('T',' ')} UTC")
        with col2:
            st.markdown(status_label(j['status']))
            st.caption(j.get('stage',''))
        if j['status'] in ('running','queued'):
            st.progress(0, text='Running in a separate process — stage updates appear below.')
        if j.get('error'):
            st.error(j['error'])
        if j['status'] == 'completed':
            for video in videos_for(jid):
                rel = video.relative_to(job_dir(jid))
                st.write(f'🎬 {rel.name}')
                if video.stat().st_size < 500_000_000:
                    st.video(str(video))
                    with video.open('rb') as f:
                        st.download_button('Download MP4', data=f, file_name=video.name, mime='video/mp4', key=f'dl_{jid}_{video.name}_{str(rel)}')
                else:
                    st.warning('Video is too large for the in-app download control. Ask your administrator for an authorized download route.')
            if j['stages'] != 'full':
                st.success('Selected pipeline stage completed. No MP4 was requested.')
        with st.expander('Detailed logs and generated files'):
            if j['status'] in ('queued','running'):
                st.caption('Use Refresh jobs at the top of the page to see newly written log lines.')
            st.code(tail_log(jid), language='text')
            for p in j.get('parts', []):
                yml = job_dir(jid) / 'input' / f'part_{p:02d}_animations.yaml'
                inv = job_dir(jid) / 'input' / 'inventories' / f'part_{p:02d}.json'
                for filename, mimetype in ((inv,'application/json'),(yml,'application/x-yaml')):
                    if filename.is_file():
                        st.download_button(f'Download {filename.name}', data=filename.read_bytes(), file_name=filename.name, mime=mimetype, key=f'{jid}_{filename.name}')


if page == 'Dashboard':
    headline('Turn complex questions into animated lessons.',
             'From PDF to narrated, high-quality educational videos using Manim.')
    st.markdown('### Overview')
    failed = [j for j in jobs if j.get('status') == 'failed']
    a,b,c,d = st.columns(4)
    a.metric('Total Jobs',len(jobs))
    b.metric('Completed',len(completed))
    c.metric('In Progress',len(active))
    d.metric('Failed',len(failed))
    st.write('')
    left,right = st.columns([1,1])
    with left:
        st.markdown('''<div class="feature-card"><div style="font-size:26px">📄</div>
        <h3>Create a New Video</h3>
        <p>Upload a PDF workbook to generate an animated, narrated lesson.</p></div>''',unsafe_allow_html=True)
        if st.button('＋ Create Video',type='primary',use_container_width=True):
            st.session_state.page='Create Video';st.rerun()
    with right:
        st.markdown('''<div class="feature-card"><div style="font-size:26px">🖼️</div>
        <h3>Other Input Options (Future)</h3>
        <p>Take a photo, upload an image, or type a question to generate a video.</p>
        <div style="font-size:12px;color:#514cc7;font-weight:700">📷 Take Photo &nbsp;·&nbsp; 🖼️ Upload Image &nbsp;·&nbsp; ⌨️ Type Question</div></div>''',unsafe_allow_html=True)
    st.markdown('### Recent activity')
    if not jobs: st.info('No jobs yet. Start with Create Video.')
    for j in jobs[:4]: job_view(j)

elif page == 'Create Video':
    headline('Create a new video','From a workbook to a narrated educational video in a few steps.')
    st.markdown('<span class="pill">01 · INPUT SOURCE</span>',unsafe_allow_html=True)
    source = st.radio('Choose an input method', ['📄 PDF workbook', '🖼️ Upload image · Soon', '📷 Take photo · Soon', '⌨️ Type question · Soon'], horizontal=True)
    if source != '📄 PDF workbook':
        st.info('This input method is reserved in the interface but not connected to the current PDF-only generation script. Select PDF workbook to submit a job.')
    st.markdown('#### Upload your workbook')
    pdf = st.file_uploader('Choose a PDF', type=['pdf'], disabled=(source!='📄 PDF workbook'), help=f'Up to {MAX_UPLOAD_MB} MB. The generator currently expects a workbook with compatible physics section definitions.')
    st.caption('⚠️ This generator is configured for a particular physics workbook structure. An arbitrary uploaded PDF may not match its section/page definitions; adjust your sections configuration before using a different workbook.')
    st.markdown('<span class="pill">02 · CONFIGURATION</span>',unsafe_allow_html=True)
    subject=st.selectbox('Subject',['Physics','Mathematics · Coming soon','Chemistry · Coming soon'])
    c1,c2=st.columns(2)
    with c1:
        parts=st.text_input('Workbook part(s)',value='1',help='Example: 1 or 2-4 or 1,3,5; these must exist in the configured physics sections file.')
        quality=st.selectbox('Video quality',['Low · 480p','Medium · 720p','High · 1080p','4K'],index=0)
    with c2:
        model=st.text_input('OpenRouter model',value=os.environ.get('OPENROUTER_MODEL','deepseek/deepseek-v4.1-flash-1017'))
        stage=st.selectbox('Workflow',['Full pipeline → MP4','Generate inventory only','Generate and validate YAML'])
    with st.expander('How the pipeline works'):
        st.markdown('**Question extraction → inventory → solution YAML → validation → Manim + narration → MP4**. Generation uses your existing Python CLI files. Runs execute in separate local processes and write progress/logs to persistent per-job directories.')
    st.markdown('<span class="pill">03 · GENERATE</span>',unsafe_allow_html=True)
    ready = pdf is not None and subject=='Physics' and source=='📄 PDF workbook'
    if st.button('✨ Start generation',type='primary',disabled=not ready,use_container_width=True):
        try:
            lookup={'Low · 480p':'l','Medium · 720p':'m','High · 1080p':'h','4K':'k'}
            modes={'Full pipeline → MP4':'full','Generate inventory only':'inventory','Generate and validate YAML':'yaml'}
            j=create_job(pdf.name,pdf.getvalue(),parts,model,lookup[quality],modes[stage])
            st.session_state.selected_job=j['id']
            st.success(f'Job submitted: {j["id"][:12]}')
            st.session_state.page='My Jobs';st.rerun()
        except (ValueError,OSError,RuntimeError) as exc:
            st.error(str(exc))

elif page == 'My Jobs':
    headline('My Jobs','Track every extraction, YAML generation and video render.')
    if st.button('↻ Refresh jobs'):
        st.rerun()
    if not jobs: st.info('No jobs submitted yet.')
    for j in jobs: job_view(j)

elif page == 'Video Library':
    headline('Video library','Your completed animations, ready for preview or download.')
    available=[j for j in jobs if videos_for(j['id'])]
    if not available: st.info('Rendered videos will appear here after a successful job.')
    for j in available: job_view(j)

else:
    headline('Settings','Environment checks and current backend integration.')
    with st.container(border=True):
        st.markdown('**Pipeline configuration**')
        st.write('Generator directory:',str(SCRIPT_ROOT))
        st.write('Renderer:',str(RENDERER))
        st.write('Jobs directory:',str(Path(os.environ.get('OGILVIE_JOBS_DIR','/jobs'))))
        for label,key in [('Generator','generate_physics_parts.py'),('Render entrypoint','render_videos.py')]:
            found=(SCRIPT_ROOT/key).is_file()
            st.write(('✅' if found else '❌'),label)
        st.write(('✅' if RENDERER.is_file() else '❌'),'Manim scene renderer')
        st.write(('✅' if os.environ.get('OPENROUTER_API_KEY') else '❌'),'OPENROUTER_API_KEY present')
        st.write(('✅' if os.environ.get('OPENAI_API_KEY') else '⚠️'),'OPENAI_API_KEY present (may be required for narration)')
    st.warning('This proof-of-concept does not include user accounts or isolation. Do not expose it publicly until authenticated access, authorization, resource limits and a managed job queue are implemented.')
