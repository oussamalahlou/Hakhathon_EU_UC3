// Config
const UPLOAD_URL = '/api/requests'; // change to your backend endpoint
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB per file
const MAX_TOTAL_SIZE = 50 * 1024 * 1024; // 50 MB total

// Elements
const form = document.getElementById('requestForm');
const descriptionEl = document.getElementById('description');
const submitBtn = document.getElementById('submitBtn');
const messageEl = document.getElementById('message');
const lambdaResponseEl = document.getElementById('lambdaResponse');
const progressWrap = document.getElementById('progressWrap');

function humanSize(bytes){
  if(bytes < 1024) return bytes + ' B';
  if(bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/(1024*1024)).toFixed(2) + ' MB';
}

function escapeHtml(s){ return s.replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

function showMessage(text, type){
  messageEl.textContent = text;
  messageEl.className = 'message ' + (type === 'error' ? 'error' : (type === 'success' ? 'success' : ''));
}

form.addEventListener('submit', e => {
  e.preventDefault();
  submit();
});

function validate(){
  const desc = descriptionEl.value.trim();
  if(!desc){ 
    showMessage('Please enter a description of your needs.', 'error'); 
    descriptionEl.focus(); 
    return false; 
  }
  return true;
}

async function submit(){
  if(!validate()) return;

  submitBtn.disabled = true;
  showMessage('Sending description to backend...', '');

  // Build payload with only description
  const payload = {
    description: descriptionEl.value.trim(),
  };

  // Send to Lambda via proxy
  const LAMBDA_PROXY = '/api/lambda';
  try{
    const resp = await fetch(LAMBDA_PROXY, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: JSON.stringify(payload) })
    });
    
    let bodyText = '';
    try{
      const json = await resp.json();
      bodyText = JSON.stringify(json, null, 2);
    }catch(e){
      try{ bodyText = await resp.text(); }catch(e2){ bodyText = ''; }
    }

    submitBtn.disabled = false;

    if(!resp.ok){
      showMessage(`Error: ${resp.status}`, 'error');
      if(lambdaResponseEl) lambdaResponseEl.textContent = `Error ${resp.status}: ${bodyText}`;
    } else {
      showMessage('Request sent successfully!', 'success');
      if(lambdaResponseEl) lambdaResponseEl.textContent = `Response: ${bodyText}`;
      descriptionEl.value = '';
    }
  }catch(err){
    submitBtn.disabled = false;
    console.error('Error', err);
    showMessage('Network error: ' + (err && err.message ? err.message : String(err)), 'error');
    if(lambdaResponseEl) lambdaResponseEl.textContent = `Error: ${err && err.message ? err.message : String(err)}`;
  }
}
