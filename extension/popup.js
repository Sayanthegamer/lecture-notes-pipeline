/**
 * Lecture Notes Scribe — Extension Popup Logic
 * 
 * Controls the popup UI, communicates with the content script to extract
 * transcript + capture frames, then sends the payload to the Render backend.
 */

document.addEventListener('DOMContentLoaded', async () => {
    // ─── DOM References ───
    const settingsToggle = document.getElementById('settings-toggle');
    const settingsPanel = document.getElementById('settings-panel');
    const backendUrlInput = document.getElementById('backend-url');
    const frameIntervalInput = document.getElementById('frame-interval');
    const apiKeyInput = document.getElementById('api-key');
    const saveSettingsBtn = document.getElementById('save-settings');

    const videoDetected = document.getElementById('video-detected');
    const noVideo = document.getElementById('no-video');
    const videoTitle = document.getElementById('video-title');
    const videoDuration = document.getElementById('video-duration');
    const videoIdDisplay = document.getElementById('video-id-display');

    const captureBtn = document.getElementById('capture-btn');
    const captureBtnText = document.getElementById('capture-btn-text');

    const serverBtn = document.getElementById('server-btn');
    const serverBtnText = document.getElementById('server-btn-text');

    const copyCookiesBtn = document.getElementById('copy-cookies-btn');
    const copyCookiesText = document.getElementById('copy-cookies-text');

    const progressSection = document.getElementById('progress-section');
    const overallProgressFill = document.getElementById('overall-progress-fill');
    const progressMessage = document.getElementById('progress-message');

    const resultSection = document.getElementById('result-section');
    const resultInfo = document.getElementById('result-info');
    const openNotesBtn = document.getElementById('open-notes-btn');
    const copyMdBtn = document.getElementById('copy-md-btn');

    const errorSection = document.getElementById('error-section');
    const errorMessage = document.getElementById('error-message');
    const retryBtn = document.getElementById('retry-btn');

    // Step indicators
    const steps = {
        transcript: {
            el: document.querySelector('#step-transcript .step-indicator'),
            status: document.getElementById('step-transcript-status')
        },
        frames: {
            el: document.querySelector('#step-frames .step-indicator'),
            status: document.getElementById('step-frames-status')
        },
        upload: {
            el: document.querySelector('#step-upload .step-indicator'),
            status: document.getElementById('step-upload-status')
        },
        generate: {
            el: document.querySelector('#step-generate .step-indicator'),
            status: document.getElementById('step-generate-status')
        }
    };

    let currentVideoInfo = null;
    let storedMarkdown = '';
    let storedHtml = '';
    let activeTabId = null;

    // ─── Load Settings ───
    const syncSettings = await chrome.storage.sync.get(['backendUrl', 'frameInterval']);
    const localSettings = await chrome.storage.local.get(['apiKey']);
    
    backendUrlInput.value = syncSettings.backendUrl || 'https://lecture-notes-pipeline.onrender.com/';
    frameIntervalInput.value = syncSettings.frameInterval || 30;
    if (apiKeyInput) {
        apiKeyInput.value = localSettings.apiKey || '';
    }

    // ─── Proactive API Key Validation ───
    function validateApiKey() {
        if (!apiKeyInput.value.trim()) {
            captureBtn.disabled = true;
            if (serverBtn) serverBtn.disabled = true;
            errorMessage.textContent = "Please open Settings and configure your API Key first.";
            errorSection.classList.remove('hidden');
            return false;
        }
        errorSection.classList.add('hidden');
        return true;
    }

    // ─── Settings Toggle ───
    settingsToggle.addEventListener('click', () => {
        settingsPanel.classList.toggle('hidden');
    });

    saveSettingsBtn.addEventListener('click', async () => {
        let url = backendUrlInput.value.trim();
        if (url && !url.endsWith('/')) url += '/';
        backendUrlInput.value = url;

        await chrome.storage.sync.set({
            backendUrl: url,
            frameInterval: parseInt(frameIntervalInput.value) || 30
        });
        await chrome.storage.sync.remove('apiKey');

        await chrome.storage.local.set({
            apiKey: apiKeyInput ? apiKeyInput.value.trim() : ''
        });

        settingsPanel.classList.add('hidden');
        validateApiKey();
        if (currentVideoInfo) {
            showVideoDetected(currentVideoInfo); // re-evaluate button state
        }
    });

    // ─── Detect Current Tab's Video ───
    async function detectVideo() {
        try {
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (!tab || !tab.url || !tab.url.includes('youtube.com/watch')) {
                showNoVideo();
                return;
            }

            activeTabId = tab.id;

            // Send message to content script
            chrome.tabs.sendMessage(tab.id, { action: 'getVideoInfo' }, (response) => {
                if (chrome.runtime.lastError) {
                    // Content script not loaded yet — inject it
                    chrome.scripting.executeScript({
                        target: { tabId: tab.id },
                        files: ['content.js']
                    }, () => {
                        // Retry after injection
                        setTimeout(() => {
                            chrome.tabs.sendMessage(tab.id, { action: 'getVideoInfo' }, handleVideoInfo);
                        }, 500);
                    });
                    return;
                }
                handleVideoInfo(response);
            });
        } catch (e) {
            console.error('Video detection failed:', e);
            showNoVideo();
        }
    }

    function handleVideoInfo(response) {
        if (response && response.success) {
            currentVideoInfo = response.data;
            showVideoDetected(currentVideoInfo);
        } else {
            showNoVideo();
        }
    }

    function showVideoDetected(info) {
        noVideo.classList.add('hidden');
        videoDetected.classList.remove('hidden');
        videoTitle.textContent = info.title || 'Untitled Video';
        videoDuration.textContent = `⏱ ${info.durationFormatted || '--:--:--'}`;
        videoIdDisplay.textContent = `🔗 ${info.videoId || '---'}`;
        
        if (validateApiKey()) {
            captureBtn.disabled = false;
            if (serverBtn) serverBtn.disabled = false;
        }
    }

    function showNoVideo() {
        videoDetected.classList.add('hidden');
        noVideo.classList.remove('hidden');
        captureBtn.disabled = true;
        if (serverBtn) serverBtn.disabled = true;
    }

    // ─── Step UI Helpers ───
    function setStep(stepName, state, statusText) {
        const step = steps[stepName];
        if (!step) return;

        step.el.className = 'step-indicator ' + state; // 'pending', 'active', 'done', 'error'
        step.status.textContent = statusText;
    }

    function setProgress(percent, msg) {
        overallProgressFill.style.width = `${percent}%`;
        if (msg) progressMessage.textContent = msg;
    }

    // ─── Popup-side Transcript Extraction (MAIN world) ───

    function formatTimePop(totalSeconds) {
        const h = Math.floor(totalSeconds / 3600);
        const m = Math.floor((totalSeconds % 3600) / 60);
        const s = Math.floor(totalSeconds % 60);
        return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    /**
     * Extracts transcript by running code in the page's MAIN world via chrome.scripting.
     * This bypasses both the isolated world limitation AND YouTube's CSP.
     * Returns { success, transcript, lineCount } or { success: false, error }.
     */
    async function extractTranscriptFromPopup(tabId) {
        try {
            // Step A: Execute in MAIN world to get caption track URLs from YouTube's JS context
            const injectionResults = await chrome.scripting.executeScript({
                target: { tabId: tabId },
                world: 'MAIN',
                func: () => {
                    // This runs in YouTube's page context — full access to page JS variables
                    try {
                        let playerResponse = window.ytInitialPlayerResponse;

                        // Fallback: try the movie_player element's API
                        if (!playerResponse) {
                            const player = document.querySelector('#movie_player');
                            if (player && typeof player.getPlayerResponse === 'function') {
                                playerResponse = player.getPlayerResponse();
                            }
                        }

                        if (!playerResponse) {
                            return { error: 'No player response found in page context' };
                        }

                        const captionTracks = playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
                        if (!captionTracks || captionTracks.length === 0) {
                            return { error: 'No caption tracks in player response' };
                        }

                        return {
                            tracks: captionTracks.map(t => ({
                                baseUrl: t.baseUrl,
                                languageCode: t.languageCode,
                                name: t.name ? (t.name.simpleText || t.name.runs?.map(r => r.text).join('') || '') : '',
                                kind: t.kind || ''
                            }))
                        };
                    } catch (e) {
                        return { error: 'MAIN world error: ' + e.message };
                    }
                }
            });

            const result = injectionResults?.[0]?.result;
            if (!result || result.error || !result.tracks || result.tracks.length === 0) {
                console.log('MAIN world caption extraction failed:', result?.error || 'No tracks');
                return { success: false, error: result?.error || 'No caption tracks found' };
            }

            console.log(`Found ${result.tracks.length} caption tracks:`, result.tracks.map(t => t.languageCode));

            // Step B: Pick the best track (prefer English, then Hindi, then first available)
            const tracks = result.tracks;
            let selectedTrack = tracks.find(t => t.languageCode === 'en') ||
                                tracks.find(t => t.languageCode?.startsWith('en')) ||
                                tracks.find(t => t.languageCode === 'hi') ||
                                tracks[0];

            let captionUrl = selectedTrack.baseUrl;
            // Request JSON3 format
            if (!captionUrl.includes('fmt=')) {
                captionUrl += '&fmt=json3';
            } else {
                captionUrl = captionUrl.replace(/fmt=\w+/, 'fmt=json3');
            }

            console.log(`Fetching captions from ${selectedTrack.languageCode} track...`);

            // Step C: Fetch the actual caption data (popup can fetch YouTube URLs thanks to host_permissions)
            const captionResponse = await fetch(captionUrl);
            if (!captionResponse.ok) {
                return { success: false, error: `Caption fetch failed: HTTP ${captionResponse.status}` };
            }

            const captionData = await captionResponse.json();
            if (!captionData.events) {
                return { success: false, error: 'No events in caption JSON3 response' };
            }

            // Step D: Parse into formatted transcript lines
            const lines = [];
            for (const event of captionData.events) {
                if (event.segs) {
                    const text = event.segs.map(s => s.utf8 || '').join('').trim();
                    if (text) {
                        const startSec = (event.tStartMs || 0) / 1000;
                        lines.push(`[${formatTimePop(startSec)}] ${text}`);
                    }
                }
            }

            if (lines.length === 0) {
                return { success: false, error: 'Caption data contained no text entries' };
            }

            console.log(`Successfully extracted ${lines.length} transcript lines via MAIN world injection`);
            return {
                success: true,
                transcript: lines.join('\n'),
                lineCount: lines.length
            };

        } catch (e) {
            console.error('extractTranscriptFromPopup error:', e);
            return { success: false, error: 'Popup extraction error: ' + e.message };
        }
    }

    /**
     * Retrieves YouTube cookies using chrome.cookies API and formats them
     * in the Netscape cookie file format.
     */
    async function getYoutubeCookies() {
        return new Promise((resolve) => {
            if (!chrome.cookies) {
                console.warn("chrome.cookies API is not available.");
                resolve(null);
                return;
            }
            chrome.cookies.getAll({ domain: 'youtube.com' }, (cookies) => {
                if (!cookies || cookies.length === 0) {
                    console.log("No cookies found for youtube.com");
                    resolve(null);
                    return;
                }
                
                let cookieFileContent = "# Netscape HTTP Cookie File\n";
                cookieFileContent += "# This file was automatically generated by Lecture Notes Scribe\n\n";
                
                for (const cookie of cookies) {
                    const domain = cookie.domain;
                    const flag = domain.startsWith('.') ? 'TRUE' : 'FALSE';
                    const path = cookie.path;
                    const secure = cookie.secure ? 'TRUE' : 'FALSE';
                    const expiration = cookie.expirationDate ? Math.round(cookie.expirationDate) : Math.round(Date.now() / 1000 + 3600 * 24 * 365);
                    const name = cookie.name;
                    const value = cookie.value;
                    
                    cookieFileContent += `${domain}\t${flag}\t${path}\t${secure}\t${expiration}\t${name}\t${value}\n`;
                }
                
                resolve(cookieFileContent);
            });
        });
    }

    // ─── Capture & Generate Flow (Browser Canvas) ───
    captureBtn.addEventListener('click', async () => {
        if (!currentVideoInfo || !activeTabId) return;

        // Reset UI
        captureBtn.disabled = true;
        serverBtn.disabled = true;
        copyCookiesBtn.disabled = true;
        captureBtnText.textContent = 'Processing...';
        progressSection.classList.remove('hidden');
        resultSection.classList.add('hidden');
        errorSection.classList.add('hidden');

        // Ensure steps are visible and labeled correctly for browser flow
        document.getElementById('step-transcript').classList.remove('hidden');
        document.getElementById('step-frames').classList.remove('hidden');
        document.querySelector('#step-upload .step-label').textContent = 'Send to Backend';
        document.querySelector('#step-generate .step-label').textContent = 'Generate Notes';

        setStep('transcript', 'pending', 'Pending');
        setStep('frames', 'pending', 'Pending');
        setStep('upload', 'pending', 'Pending');
        setStep('generate', 'pending', 'Pending');
        setProgress(0, 'Starting capture...');

        try {
            // ── Step 1: Extract Transcript ──
            setStep('transcript', 'active', 'Extracting...');
            setProgress(5, 'Extracting transcript from YouTube captions...');

            // PRIMARY: Use chrome.scripting.executeScript in MAIN world to access
            // YouTube's page-level JS variables (bypasses CSP and isolated world)
            let transcriptResult = await extractTranscriptFromPopup(activeTabId);

            // FALLBACK: Try content script methods if popup extraction failed
            if (!transcriptResult || !transcriptResult.success) {
                console.log('Popup extraction failed, falling back to content script methods...');
                setProgress(8, 'Trying alternative transcript extraction methods...');
                transcriptResult = await sendTabMessage(activeTabId, { action: 'extractTranscript' });
            }

            if (!transcriptResult || !transcriptResult.success) {
                setStep('transcript', 'error', 'Failed');
                throw new Error(`Transcript extraction failed: ${transcriptResult?.error || 'Unknown error'}`);
            }

            setStep('transcript', 'done', `${transcriptResult.lineCount} lines`);
            setProgress(15, `Transcript extracted (${transcriptResult.lineCount} lines). Capturing frames...`);

            // ── Step 2: Capture Keyframes ──
            setStep('frames', 'active', 'Scanning...');

            const interval = parseInt(frameIntervalInput.value) || 30;
            const framesResult = await sendTabMessage(activeTabId, { action: 'captureFrames', interval });

            if (!framesResult || !framesResult.success) {
                setStep('frames', 'error', 'Failed');
                throw new Error(`Frame capture failed: ${framesResult?.error || 'Unknown error'}`);
            }

            setStep('frames', 'done', `${framesResult.count} frames`);
            setProgress(40, `Captured ${framesResult.count} keyframes. Sending to backend...`);

            // ── Step 3: Upload to Backend ──
            setStep('upload', 'active', 'Uploading...');

            const backendUrl = backendUrlInput.value.trim();
            if (!backendUrl) {
                throw new Error('Backend URL is not configured. Open settings and enter your Render URL.');
            }

            const payload = {
                url: currentVideoInfo.url,
                transcript: transcriptResult.transcript,
                frames: framesResult.frames.map(f => ({
                    timestamp: f.timestamp,
                    filename: f.filename,
                    data: f.data
                }))
            };

            const { apiKey = '' } = await chrome.storage.local.get(['apiKey']);
            const apiUrl = `${backendUrl}api/generate-from-capture`;
            const response = await fetch(apiUrl, {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-API-Key': apiKey
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                const errData = await response.json().catch(() => ({}));
                throw new Error(errData.detail || `Server responded with ${response.status}`);
            }

            const jobData = await response.json();
            const jobId = jobData.job_id;

            setStep('upload', 'done', 'Sent');
            setProgress(50, 'Payload uploaded. Gemini is generating notes...');

            // ── Step 4: Poll for Generation Result ──
            setStep('generate', 'active', 'Processing...');

            chrome.runtime.sendMessage({ action: 'startJob', jobId, backendUrl, apiKey });
            await pollJobStatus(backendUrl, jobId, false, apiKey);

        } catch (error) {
            console.error('Pipeline error:', error);
            errorMessage.textContent = error.message;
            errorSection.classList.remove('hidden');
        } finally {
            captureBtn.disabled = false;
            serverBtn.disabled = false;
            copyCookiesBtn.disabled = false;
            captureBtnText.textContent = 'Browser Canvas Capture';
        }
    });

    // ─── Server-Side Processing (Auto-Cookies) Flow ───
    serverBtn.addEventListener('click', async () => {
        if (!currentVideoInfo) return;

        // Reset UI
        serverBtn.disabled = true;
        captureBtn.disabled = true;
        copyCookiesBtn.disabled = true;
        serverBtnText.textContent = 'Processing...';
        progressSection.classList.remove('hidden');
        resultSection.classList.add('hidden');
        errorSection.classList.add('hidden');

        // Dynamically customize steps for server flow (only 2 steps)
        document.getElementById('step-transcript').classList.add('hidden');
        document.getElementById('step-frames').classList.add('hidden');
        
        // Update Step labels
        document.querySelector('#step-upload .step-label').textContent = 'Send URL & Session';
        document.querySelector('#step-generate .step-label').textContent = 'Generate Notes on Server';

        setStep('upload', 'pending', 'Pending');
        setStep('generate', 'pending', 'Pending');
        setProgress(0, 'Extracting browser session cookies...');

        try {
            // ── Step 1: Extract Cookies ──
            setStep('upload', 'active', 'Extracting...');
            setProgress(10, 'Retrieving YouTube session cookies...');
            const cookiesText = await getYoutubeCookies();
            
            setProgress(25, 'Connecting to server...');
            setStep('upload', 'active', 'Uploading...');

            const backendUrl = backendUrlInput.value.trim();
            if (!backendUrl) {
                throw new Error('Backend URL is not configured. Open settings and enter your Render URL.');
            }

            const { apiKey = '' } = await chrome.storage.local.get(['apiKey']);
            // POST to api/generate with URL + cookies
            const response = await fetch(`${backendUrl}api/generate`, {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-API-Key': apiKey
                },
                body: JSON.stringify({
                    url: currentVideoInfo.url,
                    cookies: cookiesText || null
                })
            });

            if (!response.ok) {
                const errData = await response.json().catch(() => ({}));
                throw new Error(errData.detail || `Server responded with ${response.status}`);
            }

            const jobData = await response.json();
            const jobId = jobData.job_id;

            setStep('upload', 'done', 'Sent');
            setProgress(35, 'Job accepted. Server-side processing initialized...');

            // ── Step 2: Poll status ──
            setStep('generate', 'active', 'Processing...');
            
            chrome.runtime.sendMessage({ action: 'startJob', jobId, backendUrl, apiKey });
            await pollJobStatus(backendUrl, jobId, true, apiKey); // true for isServerFlow

        } catch (error) {
            console.error('Server pipeline error:', error);
            errorMessage.textContent = error.message;
            errorSection.classList.remove('hidden');
        } finally {
            serverBtn.disabled = false;
            captureBtn.disabled = false;
            copyCookiesBtn.disabled = false;
            serverBtnText.textContent = 'Generate via Server (Fast)';
            
            // Restore step visibility and labels
            document.getElementById('step-transcript').classList.remove('hidden');
            document.getElementById('step-frames').classList.remove('hidden');
            document.querySelector('#step-upload .step-label').textContent = 'Send to Backend';
            document.querySelector('#step-generate .step-label').textContent = 'Generate Notes';
        }
    });

    // ─── Copy Cookies Flow ───
    copyCookiesBtn.addEventListener('click', async () => {
        const originalText = copyCookiesText.textContent;
        copyCookiesText.textContent = 'Copying...';
        try {
            const cookiesText = await getYoutubeCookies();
            if (!cookiesText) {
                throw new Error("No YouTube cookies found. Make sure you are logged into YouTube in this browser.");
            }
            await navigator.clipboard.writeText(cookiesText);
            copyCookiesText.textContent = 'Copied to Clipboard! ✅';
            setTimeout(() => {
                copyCookiesText.textContent = originalText;
            }, 3000);
        } catch (e) {
            console.error(e);
            alert(e.message || "Failed to copy cookies.");
            copyCookiesText.textContent = originalText;
        }
    });

    // ─── Tab Message Helper (promisified) ───
    function sendTabMessage(tabId, message) {
        return new Promise((resolve) => {
            chrome.tabs.sendMessage(tabId, message, (response) => {
                if (chrome.runtime.lastError) {
                    resolve({ success: false, error: chrome.runtime.lastError.message });
                } else {
                    resolve(response);
                }
            });
        });
    }

    // ─── Poll Job Status ───
    async function pollJobStatus(backendUrl, jobId, isServerFlow = false, apiKey = '') {
        const pollInterval = 5000; // 5 seconds
        const maxAttempts = 360;   // 30 minutes max
        let currentStatus = 'processing';

        for (let i = 0; i < maxAttempts && currentStatus === 'processing'; i++) {
            await new Promise(r => setTimeout(r, pollInterval));

            try {
                const response = await fetch(`${backendUrl}api/status/${jobId}`, {
                    headers: { 'X-API-Key': apiKey }
                });
                if (!response.ok) continue;

                const data = await response.json();
                currentStatus = data.status;

                // Sync state to local storage for the UI to pick up via the listener below
                await chrome.storage.local.set({
                    jobStatus: data.status,
                    jobProgress: data.progress,
                    jobMessage: data.message,
                    jobMarkdown: data.markdown,
                    jobHtml: data.html
                });
            } catch (e) {
                setProgress(null, 'Connection lost. Retrying...');
            }
        }
    }

    // ─── Listen for storage changes to render UI statelessly ───
    chrome.storage.onChanged.addListener((changes, namespace) => {
        if (namespace === 'local' && progressSection && !progressSection.classList.contains('hidden')) {
            if (changes.jobProgress || changes.jobMessage || changes.jobStatus) {
                chrome.storage.local.get(['jobProgress', 'jobMessage', 'jobStatus', 'jobMarkdown', 'jobHtml'], (data) => {
                    const rawProgress = data.jobProgress || 0;
                    setProgress(rawProgress, data.jobMessage);
                    
                    if (data.jobStatus === 'processing') {
                        setStep('generate', 'active', `${rawProgress}%`);
                    } else if (data.jobStatus === 'completed') {
                        setStep('generate', 'done', 'Complete');
                        setProgress(100, 'Notes compiled successfully!');
        
                        storedMarkdown = data.jobMarkdown || '';
                        storedHtml = data.jobHtml || '';
        
                        resultInfo.textContent = `Generated ${storedMarkdown.length.toLocaleString()} characters of study notes.`;
                        resultSection.classList.remove('hidden');
                        progressSection.classList.add('hidden');
                    } else if (data.jobStatus === 'failed') {
                        setStep('generate', 'error', 'Failed');
                        errorMessage.textContent = data.jobMessage || 'Server-side notes compilation failed.';
                        errorSection.classList.remove('hidden');
                    }
                });
            }
        }
    });

    // ─── Result Actions ───
    openNotesBtn.addEventListener('click', () => {
        if (!storedHtml && !storedMarkdown) return;

        // Create a blob URL and open it in a new tab
        const content = storedHtml || `<html><body><pre>${storedMarkdown}</pre></body></html>`;
        const blob = new Blob([content], { type: 'text/html' });
        const url = URL.createObjectURL(blob);
        chrome.tabs.create({ url });
    });

    copyMdBtn.addEventListener('click', async () => {
        if (!storedMarkdown) return;
        try {
            await navigator.clipboard.writeText(storedMarkdown);
            copyMdBtn.textContent = 'Copied!';
            setTimeout(() => { copyMdBtn.textContent = 'Copy Markdown'; }, 2000);
        } catch (e) {
            console.error('Copy failed:', e);
        }
    });

    retryBtn.addEventListener('click', () => {
        errorSection.classList.add('hidden');
        progressSection.classList.add('hidden');
    });

    // ─── Listen for capture progress from content script ───
    chrome.runtime.onMessage.addListener((message) => {
        if (message.type === 'captureProgress') {
            const capturePercent = 15 + (message.percent / 100) * 25; // Scale to 15-40% of overall
            setProgress(capturePercent, message.message);
            setStep('frames', 'active', `${message.percent}%`);
        }
    });

    // ─── Initialize ───
    await detectVideo();
});
