/**
 * Lecture Notes Scribe — YouTube Content Script
 * 
 * Injected into YouTube watch pages. Handles:
 * 1. Detecting the current video and extracting metadata
 * 2. Extracting captions/transcript from YouTube's timed text API
 * 3. Capturing keyframe screenshots from the <video> element via canvas
 * 
 * Communicates with popup.js via chrome.runtime.onMessage.
 * 
 * KEY ARCHITECTURE NOTE:
 * Content scripts run in Chrome's "isolated world" and CANNOT access page-level
 * JavaScript variables (like ytInitialPlayerResponse). To access YouTube's player
 * config, we inject a <script> into the page DOM that reads the variable and posts
 * the caption track URLs back via window.postMessage.
 */

(() => {
    "use strict";

    // ─── Utility ───

    function formatTime(totalSeconds) {
        const h = Math.floor(totalSeconds / 3600);
        const m = Math.floor((totalSeconds % 3600) / 60);
        const s = Math.floor(totalSeconds % 60);
        return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    function getVideoId() {
        const params = new URLSearchParams(window.location.search);
        return params.get('v');
    }

    function getVideoElement() {
        return document.querySelector('video.html5-main-video') || document.querySelector('video');
    }

    // ─── Transcript Extraction ───

    /**
     * Main transcript extraction function with multiple fallback methods.
     */
    async function extractTranscript() {
        // Method 1: Inject into page context to get caption track URLs from ytInitialPlayerResponse
        console.log('[Scribe] Trying Method 1: Page context injection for timedtext API...');
        try {
            const transcript = await fetchFromPageContext();
            if (transcript && transcript.length > 0) {
                console.log(`[Scribe] Method 1 succeeded: ${transcript.length} entries`);
                return transcript;
            }
        } catch (e) {
            console.log('[Scribe] Method 1 failed:', e.message);
        }

        // Method 2: Scrape caption track URL from the raw page HTML source
        console.log('[Scribe] Trying Method 2: HTML source scraping for timedtext URL...');
        try {
            const transcript = await fetchFromPageSource();
            if (transcript && transcript.length > 0) {
                console.log(`[Scribe] Method 2 succeeded: ${transcript.length} entries`);
                return transcript;
            }
        } catch (e) {
            console.log('[Scribe] Method 2 failed:', e.message);
        }

        // Method 3: Try reading from the video element's text tracks
        console.log('[Scribe] Trying Method 3: HTML5 TextTrack API...');
        try {
            const transcript = extractFromTextTracks();
            if (transcript && transcript.length > 0) {
                console.log(`[Scribe] Method 3 succeeded: ${transcript.length} entries`);
                return transcript;
            }
        } catch (e) {
            console.log('[Scribe] Method 3 failed:', e.message);
        }

        // Method 4: Try reading from the transcript panel in the DOM
        console.log('[Scribe] Trying Method 4: YouTube transcript panel DOM scraping...');
        try {
            const transcript = await extractFromTranscriptPanel();
            if (transcript && transcript.length > 0) {
                console.log(`[Scribe] Method 4 succeeded: ${transcript.length} entries`);
                return transcript;
            }
        } catch (e) {
            console.log('[Scribe] Method 4 failed:', e.message);
        }

        // Method 5: Construct timedtext URL manually from video ID
        console.log('[Scribe] Trying Method 5: Direct timedtext URL construction...');
        try {
            const transcript = await fetchFromDirectTimedText();
            if (transcript && transcript.length > 0) {
                console.log(`[Scribe] Method 5 succeeded: ${transcript.length} entries`);
                return transcript;
            }
        } catch (e) {
            console.log('[Scribe] Method 5 failed:', e.message);
        }

        return null;
    }

    /**
     * Method 1: Injects a <script> tag into the page DOM to access ytInitialPlayerResponse
     * from YouTube's page-level JavaScript context. Results are passed back via window.postMessage.
     */
    function fetchFromPageContext() {
        return new Promise((resolve, reject) => {
            const timeoutId = setTimeout(() => {
                window.removeEventListener('message', handler);
                reject(new Error('Page context injection timed out'));
            }, 5000);

            function handler(event) {
                if (event.data && event.data.type === 'SCRIBE_CAPTION_TRACKS') {
                    window.removeEventListener('message', handler);
                    clearTimeout(timeoutId);

                    if (event.data.error) {
                        reject(new Error(event.data.error));
                        return;
                    }

                    const tracks = event.data.tracks;
                    if (!tracks || tracks.length === 0) {
                        reject(new Error('No caption tracks returned from page context'));
                        return;
                    }

                    // Use the tracks to fetch the actual captions
                    fetchCaptionFromTrack(tracks)
                        .then(resolve)
                        .catch(reject);
                }
            }

            window.addEventListener('message', handler);

            // Inject a script that runs in the page's JavaScript context
            const script = document.createElement('script');
            script.textContent = `
                (function() {
                    try {
                        // Access the player response from the page's global scope
                        let playerResponse = window.ytInitialPlayerResponse;
                        
                        // If not on window, try to find it in the ytplayer config
                        if (!playerResponse) {
                            const player = document.querySelector('#movie_player');
                            if (player && player.getPlayerResponse) {
                                playerResponse = player.getPlayerResponse();
                            }
                        }
                        
                        // Try ytcfg as another source
                        if (!playerResponse && window.ytcfg) {
                            const data = window.ytcfg.data_;
                            if (data && data.PLAYER_VARS && data.PLAYER_VARS.embedded_player_response) {
                                try {
                                    playerResponse = JSON.parse(data.PLAYER_VARS.embedded_player_response);
                                } catch(e) {}
                            }
                        }
                        
                        if (!playerResponse) {
                            window.postMessage({
                                type: 'SCRIBE_CAPTION_TRACKS',
                                error: 'Could not find player response in page context'
                            }, '*');
                            return;
                        }
                        
                        const captions = playerResponse.captions;
                        if (!captions || !captions.playerCaptionsTracklistRenderer) {
                            window.postMessage({
                                type: 'SCRIBE_CAPTION_TRACKS',
                                error: 'No captions object in player response'
                            }, '*');
                            return;
                        }
                        
                        const captionTracks = captions.playerCaptionsTracklistRenderer.captionTracks || [];
                        
                        // Send the track info back (baseUrl, languageCode, name)
                        const tracks = captionTracks.map(t => ({
                            baseUrl: t.baseUrl,
                            languageCode: t.languageCode,
                            name: t.name ? t.name.simpleText || '' : '',
                            kind: t.kind || ''
                        }));
                        
                        window.postMessage({
                            type: 'SCRIBE_CAPTION_TRACKS',
                            tracks: tracks
                        }, '*');
                        
                    } catch(e) {
                        window.postMessage({
                            type: 'SCRIBE_CAPTION_TRACKS',
                            error: 'Page context error: ' + e.message
                        }, '*');
                    }
                })();
            `;
            document.documentElement.appendChild(script);
            script.remove(); // Clean up immediately after execution
        });
    }

    /**
     * Fetches actual caption text from the best available track URL.
     */
    async function fetchCaptionFromTrack(tracks) {
        // Prefer English, then Hindi (common for Indian lectures), then any
        let selectedTrack = tracks.find(t => t.languageCode === 'en') ||
                            tracks.find(t => t.languageCode?.startsWith('en')) ||
                            tracks.find(t => t.languageCode === 'hi') ||
                            tracks[0];

        let url = selectedTrack.baseUrl;
        // Request JSON3 format for structured timestamp data
        if (!url.includes('fmt=')) {
            url += '&fmt=json3';
        } else {
            url = url.replace(/fmt=\w+/, 'fmt=json3');
        }

        console.log(`[Scribe] Fetching captions from: ${selectedTrack.languageCode} track`);
        const response = await fetch(url);
        const data = await response.json();

        if (!data.events) {
            throw new Error('No events in caption JSON3 response');
        }

        const transcript = [];
        for (const event of data.events) {
            if (event.segs) {
                const text = event.segs.map(s => s.utf8 || '').join('').trim();
                if (text) {
                    const startSec = (event.tStartMs || 0) / 1000;
                    transcript.push({
                        start: startSec,
                        text: text,
                        timestamp: formatTime(startSec)
                    });
                }
            }
        }

        return transcript;
    }

    /**
     * Method 2: Scrapes the raw HTML page source for timedtext base URLs.
     * Content scripts CAN read the page HTML — they just can't access JS variables.
     */
    async function fetchFromPageSource() {
        // Fetch the page HTML again to get the full source with embedded JSON
        const videoId = getVideoId();
        if (!videoId) throw new Error('No video ID');

        const pageResponse = await fetch(window.location.href);
        const pageHtml = await pageResponse.text();

        // Look for captionTracks in the page HTML
        const captionMatch = pageHtml.match(/"captionTracks"\s*:\s*(\[.*?\])/s);
        if (!captionMatch) {
            throw new Error('No captionTracks found in page HTML');
        }

        let captionTracks;
        try {
            // The JSON might have escaped characters, fix common issues
            let jsonStr = captionMatch[1];
            // YouTube escapes \u0026 for & in URLs
            jsonStr = jsonStr.replace(/\\u0026/g, '&');
            captionTracks = JSON.parse(jsonStr);
        } catch (e) {
            throw new Error('Failed to parse captionTracks JSON: ' + e.message);
        }

        if (!captionTracks || captionTracks.length === 0) {
            throw new Error('Empty captionTracks array');
        }

        const tracks = captionTracks.map(t => ({
            baseUrl: t.baseUrl,
            languageCode: t.languageCode,
            name: t.name ? (t.name.simpleText || '') : '',
            kind: t.kind || ''
        }));

        return fetchCaptionFromTrack(tracks);
    }

    /**
     * Method 3: Reads captions from the <video> element's TextTrack API.
     */
    function extractFromTextTracks() {
        const video = getVideoElement();
        if (!video) throw new Error('No video element found');

        const tracks = video.textTracks;
        if (!tracks || tracks.length === 0) {
            throw new Error('No text tracks available');
        }

        // Find an English track or any track
        let track = Array.from(tracks).find(t => t.language === 'en' && t.mode !== 'disabled') ||
                    Array.from(tracks).find(t => t.language?.startsWith('en')) ||
                    Array.from(tracks)[0];

        // Activate the track if needed
        if (track.mode === 'disabled') {
            track.mode = 'hidden';
        }

        // Wait a moment for cues to populate after mode change
        const cues = track.cues;
        if (!cues || cues.length === 0) {
            throw new Error('No cues in text track');
        }

        const transcript = [];
        for (const cue of cues) {
            transcript.push({
                start: cue.startTime,
                text: (cue.text || '').replace(/[<>]/g, '').trim(),
                timestamp: formatTime(cue.startTime)
            });
        }

        return transcript;
    }

    /**
     * Method 4: Opens the transcript panel in YouTube's UI and scrapes the entries from the DOM.
     */
    async function extractFromTranscriptPanel() {
        // Try to find existing transcript segments first
        let segments = document.querySelectorAll('ytd-transcript-segment-renderer');
        
        if (segments.length === 0) {
            // Try to click "Show transcript" in the engagement panel
            // First, look for the "...more" button in description to expand it
            const moreBtn = document.querySelector('#expand') || 
                           document.querySelector('tp-yt-paper-button#expand');
            if (moreBtn) {
                moreBtn.click();
                await new Promise(r => setTimeout(r, 1000));
            }

            // Now look for "Show transcript" button
            const allButtons = document.querySelectorAll(
                'ytd-button-renderer a, ytd-button-renderer button, ' +
                '#primary-button button, .yt-spec-button-shape-next'
            );
            for (const btn of allButtons) {
                const text = btn.textContent?.toLowerCase() || '';
                const ariaLabel = btn.getAttribute('aria-label')?.toLowerCase() || '';
                if (text.includes('transcript') || ariaLabel.includes('transcript')) {
                    btn.click();
                    await new Promise(r => setTimeout(r, 2500));
                    break;
                }
            }

            segments = document.querySelectorAll('ytd-transcript-segment-renderer');
        }

        if (segments.length === 0) {
            throw new Error('Could not find transcript segments in DOM');
        }

        const transcript = [];
        for (const seg of segments) {
            const timeEl = seg.querySelector('.segment-timestamp');
            const textEl = seg.querySelector('.segment-text');
            if (timeEl && textEl) {
                const timeStr = timeEl.textContent.trim();
                const text = textEl.textContent.trim();
                // Parse MM:SS or H:MM:SS
                const parts = timeStr.split(':').map(Number);
                let seconds = 0;
                if (parts.length === 3) seconds = parts[0] * 3600 + parts[1] * 60 + parts[2];
                else if (parts.length === 2) seconds = parts[0] * 60 + parts[1];

                transcript.push({
                    start: seconds,
                    text: text,
                    timestamp: formatTime(seconds)
                });
            }
        }

        return transcript;
    }

    /**
     * Method 5: Constructs the timedtext URL directly from the video ID.
     * This is the most reliable last-resort method.
     */
    async function fetchFromDirectTimedText() {
        const videoId = getVideoId();
        if (!videoId) throw new Error('No video ID found');

        // Try common languages
        const languages = ['en', 'hi', 'en-IN'];

        for (const lang of languages) {
            try {
                // YouTube's public timedtext API endpoint
                const url = `https://www.youtube.com/api/timedtext?v=${videoId}&lang=${lang}&fmt=json3`;
                const response = await fetch(url);
                
                if (!response.ok) continue;
                
                const data = await response.json();
                if (!data.events || data.events.length === 0) continue;

                const transcript = [];
                for (const event of data.events) {
                    if (event.segs) {
                        const text = event.segs.map(s => s.utf8 || '').join('').trim();
                        if (text) {
                            const startSec = (event.tStartMs || 0) / 1000;
                            transcript.push({
                                start: startSec,
                                text: text,
                                timestamp: formatTime(startSec)
                            });
                        }
                    }
                }

                if (transcript.length > 0) {
                    console.log(`[Scribe] Direct timedtext succeeded with lang=${lang}`);
                    return transcript;
                }
            } catch (e) {
                // Try next language
            }
        }

        // Try auto-generated captions (asr=1)
        try {
            const url = `https://www.youtube.com/api/timedtext?v=${videoId}&lang=en&kind=asr&fmt=json3`;
            const response = await fetch(url);
            if (response.ok) {
                const data = await response.json();
                if (data.events && data.events.length > 0) {
                    const transcript = [];
                    for (const event of data.events) {
                        if (event.segs) {
                            const text = event.segs.map(s => s.utf8 || '').join('').trim();
                            if (text) {
                                const startSec = (event.tStartMs || 0) / 1000;
                                transcript.push({
                                    start: startSec,
                                    text: text,
                                    timestamp: formatTime(startSec)
                                });
                            }
                        }
                    }
                    if (transcript.length > 0) return transcript;
                }
            }
        } catch (e) {
            // Fall through
        }

        throw new Error('Direct timedtext API returned no results for any language');
    }

    // ─── Frame Capture ───

    /**
     * Captures keyframe screenshots from the video at fixed intervals.
     * Seeks the video to each position, waits for the frame to load, then draws to canvas.
     * 
     * @param {number} intervalSec - Seconds between captures (default 30)
     * @param {function} onProgress - Callback(percent, message) for progress updates
     * @returns {Array<{timestamp: string, timeSec: number, data: string}>}
     */
    async function captureFrames(intervalSec = 30, onProgress = null) {
        const video = getVideoElement();
        if (!video) {
            throw new Error('No video element found on page');
        }

        const duration = video.duration;
        if (!duration || !isFinite(duration)) {
            throw new Error('Could not determine video duration');
        }

        // Pause the video before seeking
        const wasPlaying = !video.paused;
        video.pause();

        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');

        // Use the video's intrinsic resolution (capped at 1280px width for size management)
        const videoWidth = video.videoWidth || 1280;
        const videoHeight = video.videoHeight || 720;
        const scale = Math.min(1, 1280 / videoWidth);
        canvas.width = Math.round(videoWidth * scale);
        canvas.height = Math.round(videoHeight * scale);

        const frames = [];
        const totalFrames = Math.ceil(duration / intervalSec);
        const originalTime = video.currentTime;

        for (let i = 0; i < totalFrames; i++) {
            const targetTime = i * intervalSec;
            if (targetTime > duration) break;

            const percent = Math.round((i / totalFrames) * 100);
            if (onProgress) {
                onProgress(percent, `Capturing frame ${i + 1}/${totalFrames} at ${formatTime(targetTime)}...`);
            }

            // Seek to the target time
            video.currentTime = targetTime;

            // Wait for the seek to complete and frame to render
            await new Promise((resolve) => {
                const onSeeked = () => {
                    video.removeEventListener('seeked', onSeeked);
                    // Small delay to ensure the frame is fully painted
                    setTimeout(resolve, 150);
                };
                video.addEventListener('seeked', onSeeked);
                // Timeout fallback in case seeked never fires
                setTimeout(resolve, 2000);
            });

            // Draw the current video frame to canvas
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

            // Convert to JPEG base64 (quality 0.85 keeps size ~30-60KB per frame)
            const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
            // Strip the data:image/jpeg;base64, prefix for transmission
            const base64Data = dataUrl.split(',')[1];

            const timestampStr = formatTime(targetTime).replace(/:/g, '_');

            frames.push({
                timestamp: formatTime(targetTime),
                timeSec: targetTime,
                filename: `frame_${String(i).padStart(3, '0')}_time_${timestampStr}.jpg`,
                data: base64Data
            });
        }

        // Restore original video position
        video.currentTime = originalTime;
        if (wasPlaying) {
            video.play();
        }

        if (onProgress) {
            onProgress(100, `Captured ${frames.length} keyframes.`);
        }

        return frames;
    }

    // ─── Video Metadata ───

    function getVideoMetadata() {
        const video = getVideoElement();
        const videoId = getVideoId();
        const titleEl = document.querySelector('h1.ytd-watch-metadata yt-formatted-string') ||
                        document.querySelector('h1.title') ||
                        document.querySelector('#title h1');
        const title = titleEl ? titleEl.textContent.trim() : document.title.replace(' - YouTube', '').trim();

        return {
            videoId: videoId,
            url: window.location.href,
            title: title,
            duration: video ? video.duration : null,
            durationFormatted: video && video.duration ? formatTime(video.duration) : 'Unknown'
        };
    }

    // ─── Message Handler ───

    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === 'getVideoInfo') {
            try {
                const metadata = getVideoMetadata();
                sendResponse({ success: true, data: metadata });
            } catch (e) {
                sendResponse({ success: false, error: e.message });
            }
            return false; // synchronous
        }

        if (request.action === 'extractTranscript') {
            extractTranscript()
                .then(transcript => {
                    if (transcript && transcript.length > 0) {
                        // Format as timestamped text
                        const formatted = transcript.map(e => `[${e.timestamp}] ${e.text}`).join('\n');
                        sendResponse({ success: true, transcript: formatted, lineCount: transcript.length });
                    } else {
                        sendResponse({ success: false, error: 'No transcript could be extracted from any method.' });
                    }
                })
                .catch(e => {
                    sendResponse({ success: false, error: e.message });
                });
            return true; // async
        }

        if (request.action === 'captureFrames') {
            const interval = request.interval || 30;
            captureFrames(interval, (percent, msg) => {
                // Send progress updates back to popup
                chrome.runtime.sendMessage({ type: 'captureProgress', percent, message: msg });
            })
                .then(frames => {
                    sendResponse({ success: true, frames: frames, count: frames.length });
                })
                .catch(e => {
                    sendResponse({ success: false, error: e.message });
                });
            return true; // async
        }

        return false;
    });

    console.log('[Lecture Notes Scribe] Content script loaded.');
})();
