/**
 * Lecture Notes Scribe — Background Service Worker
 * 
 * Manages async polling via chrome.alarms to survive MV3 5-minute lifecycle limits,
 * triggering OS notifications upon job completion.
 */

const ALARM_NAME = 'checkJobStatusAlarm';

chrome.runtime.onInstalled.addListener(() => {
    chrome.alarms.clearAll();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.action === 'startJob') {
        const { jobId, backendUrl, apiKey } = message;
        chrome.storage.local.set({
            activeJobId: jobId,
            activeJobBackendUrl: backendUrl,
            activeJobApiKey: apiKey,
            jobStatus: 'processing',
            jobProgress: 0,
            jobMessage: 'Job submitted...'
        }, () => {
            // Create an alarm to check status every 1 minute in the background
            chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
            sendResponse({ success: true });
        });
        return true; // async response
    }
});

// The background polling alarm
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name !== ALARM_NAME) return;

    const { activeJobId, activeJobBackendUrl, activeJobApiKey } = await chrome.storage.local.get([
        'activeJobId', 'activeJobBackendUrl', 'activeJobApiKey'
    ]);

    if (!activeJobId || !activeJobBackendUrl) {
        chrome.alarms.clear(ALARM_NAME);
        return;
    }

    try {
        const response = await fetch(`${activeJobBackendUrl}api/status/${activeJobId}`, {
            headers: { 'X-API-Key': activeJobApiKey || '' }
        });

        if (!response.ok) return;

        const data = await response.json();
        
        // Save state to local storage so popup can render it
        await chrome.storage.local.set({
            jobStatus: data.status,
            jobProgress: data.progress,
            jobMessage: data.message,
            jobMarkdown: data.markdown,
            jobHtml: data.html
        });

        if (data.status === 'completed') {
            chrome.alarms.clear(ALARM_NAME);
            chrome.notifications.create(`job-complete-${activeJobId}`, {
                type: 'basic',
                iconUrl: 'icons/icon128.png',
                title: 'Lecture Notes Scribe',
                message: 'Your study notes are ready!',
                buttons: [{ title: 'View Notes' }]
            });
        } else if (data.status === 'failed') {
            chrome.alarms.clear(ALARM_NAME);
            chrome.notifications.create(`job-failed-${activeJobId}`, {
                type: 'basic',
                iconUrl: 'icons/icon128.png',
                title: 'Lecture Notes Scribe',
                message: `Failed: ${data.message || 'Unknown error'}`
            });
        }
    } catch (error) {
        console.error("Background polling error:", error);
    }
});

// Listen for notification clicks to redirect to the desktop viewer
chrome.notifications.onClicked.addListener(async (notificationId) => {
    if (notificationId.startsWith('job-complete-')) {
        const jobId = notificationId.replace('job-complete-', '');
        const { activeJobBackendUrl } = await chrome.storage.local.get(['activeJobBackendUrl']);
        if (activeJobBackendUrl) {
            chrome.tabs.create({ url: `${activeJobBackendUrl}?jobId=${jobId}` });
        }
        chrome.notifications.clear(notificationId);
    }
});

chrome.notifications.onButtonClicked.addListener(async (notificationId, buttonIndex) => {
    if (notificationId.startsWith('job-complete-') && buttonIndex === 0) {
        const jobId = notificationId.replace('job-complete-', '');
        const { activeJobBackendUrl } = await chrome.storage.local.get(['activeJobBackendUrl']);
        if (activeJobBackendUrl) {
            chrome.tabs.create({ url: `${activeJobBackendUrl}?jobId=${jobId}` });
        }
        chrome.notifications.clear(notificationId);
    }
});
