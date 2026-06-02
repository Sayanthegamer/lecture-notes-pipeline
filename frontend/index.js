document.addEventListener('DOMContentLoaded', () => {
    const backendUrlInput = document.getElementById('backend-url');
    const generatorForm = document.getElementById('generator-form');
    const lectureUrlInput = document.getElementById('lecture-url');
    const submitBtn = document.getElementById('submit-btn');
    const spinner = submitBtn.querySelector('.spinner');
    const btnText = submitBtn.querySelector('.btn-text');
    
    const progressContainer = document.getElementById('progress-container');
    const statusMessage = document.getElementById('status-message');
    const progressPercent = document.getElementById('progress-percent');
    const progressBarFill = document.getElementById('progress-bar-fill');
    
    const outputActions = document.getElementById('output-actions');
    const outputPlaceholder = document.getElementById('output-placeholder');
    const outputContent = document.getElementById('output-content');
    
    const copyMdBtn = document.getElementById('copy-md-btn');
    const printBtn = document.getElementById('print-btn');
    
    let activePollInterval = null;
    let compiledMarkdown = "";

    // 1. Load saved Backend API URL or default to the current window location
    const savedUrl = localStorage.getItem('notes_scribe_backend_url');
    if (savedUrl) {
        backendUrlInput.value = savedUrl;
    } else {
        // Use the current origin as the default (works out-of-the-box when hosted together!)
        let currentOrigin = window.location.origin;
        if (!currentOrigin.endsWith('/')) {
            currentOrigin += '/';
        }
        backendUrlInput.value = currentOrigin;
    }

    // Save URL when changed
    backendUrlInput.addEventListener('change', () => {
        let url = backendUrlInput.value.trim();
        if (url && !url.endsWith('/')) {
            url += '/';
        }
        backendUrlInput.value = url;
        localStorage.setItem('notes_scribe_backend_url', url);
    });

    // Get cleaned base API url
    function getApiBase() {
        let url = backendUrlInput.value.trim();
        if (!url) {
            alert("Please enter your Render Backend API URL in the header configuration field.");
            backendUrlInput.focus();
            return null;
        }
        if (!url.endsWith('/')) {
            url += '/';
        }
        return url;
    }

    // 2. Handle Form Submission (Start Pipeline)
    generatorForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const apiBase = getApiBase();
        if (!apiBase) return;
        
        const lectureUrl = lectureUrlInput.value.trim();
        if (!lectureUrl) return;

        // Reset UI States
        submitBtn.disabled = true;
        spinner.classList.remove('hidden');
        btnText.innerText = "Submitting Job...";
        
        progressContainer.classList.remove('hidden');
        statusMessage.innerText = "Connecting to backend server...";
        updateProgress(0);

        try {
            const response = await fetch(`${apiBase}api/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: lectureUrl })
            });

            if (!response.ok) {
                const errData = await response.json();
                throw new Error(errData.detail || "Server rejected request");
            }

            const data = await response.json();
            const jobId = data.job_id;
            
            statusMessage.innerText = "Job accepted! Starting processing...";
            startPolling(jobId, apiBase);

        } catch (error) {
            console.error(error);
            alert(`Failed to start notes compilation: ${error.message}`);
            resetSubmitButton();
            progressContainer.classList.add('hidden');
        }
    });

    // Helper to update progress UI
    function updateProgress(percent) {
        progressPercent.innerText = `${percent}%`;
        progressBarFill.style.width = `${percent}%`;
    }

    // Reset button to initial state
    function resetSubmitButton() {
        submitBtn.disabled = false;
        spinner.classList.add('hidden');
        btnText.innerText = "Generate Study Guide";
    }

    // 3. Status Polling Loop
    function startPolling(jobId, apiBase) {
        if (activePollInterval) {
            clearInterval(activePollInterval);
        }

        activePollInterval = setInterval(async () => {
            try {
                const response = await fetch(`${apiBase}api/status/${jobId}`);
                if (!response.ok) {
                    throw new Error("Failed to fetch job status");
                }

                const data = await response.json();
                statusMessage.innerText = data.message;
                updateProgress(data.progress);

                if (data.status === 'completed') {
                    clearInterval(activePollInterval);
                    activePollInterval = null;
                    displayNotes(data.markdown);
                    resetSubmitButton();
                } else if (data.status === 'failed') {
                    clearInterval(activePollInterval);
                    activePollInterval = null;
                    alert(`Notes compilation failed: ${data.message}`);
                    resetSubmitButton();
                }

            } catch (error) {
                console.error(error);
                statusMessage.innerText = "Connection lost. Reconnecting...";
            }
        }, 5000); // Poll every 5 seconds
    }

    // 4. Render and Display Notes
    function displayNotes(markdownText) {
        compiledMarkdown = markdownText;

        // Clean up UI panels
        outputPlaceholder.classList.add('hidden');
        outputContent.classList.remove('hidden');
        outputActions.classList.remove('hidden');

        // Extract math blocks to protect them from marked.js parsing
        const mathBlocks = [];
        let placeholderCount = 0;
        let tempMarkdown = markdownText;

        // Display math ($$ ... $$)
        tempMarkdown = tempMarkdown.replace(/\$\$([\s\S]+?)\$\$/g, (match) => {
            const placeholder = `@@MATH_BLOCK_${placeholderCount}@@`;
            mathBlocks.push({ placeholder, content: match });
            placeholderCount++;
            return placeholder;
        });

        // Inline math ($ ... $)
        tempMarkdown = tempMarkdown.replace(/\$([^$\n]+?)\$/g, (match) => {
            const placeholder = `@@MATH_BLOCK_${placeholderCount}@@`;
            mathBlocks.push({ placeholder, content: match });
            placeholderCount++;
            return placeholder;
        });

        // Parse with marked.js
        let renderedHtml = marked.parse(tempMarkdown);

        // Restore math blocks (using arrow function replacer to prevent double-dollar bugs)
        for (const block of mathBlocks) {
            renderedHtml = renderedHtml.replace(block.placeholder, () => block.content);
        }

        // Set innerHTML
        outputContent.innerHTML = renderedHtml;

        // Post-process blockquotes for GitHub-style alerts
        outputContent.querySelectorAll('blockquote').forEach(bq => {
            const p = bq.querySelector('p');
            if (p) {
                const match = p.innerHTML.match(/^\[!(NOTE|WARNING|TIP|IMPORTANT|CAUTION)\]/i);
                if (match) {
                    const type = match[1].toUpperCase();
                    p.innerHTML = p.innerHTML.replace(/^\[!(NOTE|WARNING|TIP|IMPORTANT|CAUTION)\]\s*/i, '');
                    bq.classList.add('alert', `alert-${type.toLowerCase()}`);
                    
                    const title = document.createElement('div');
                    title.className = 'alert-title';
                    title.innerText = type;
                    bq.insertBefore(title, p);
                }
            }
        });

        // Trigger MathJax to typeset the dynamic formulas
        if (window.MathJax && typeof window.MathJax.typeset === 'function') {
            window.MathJax.typeset();
        }
        
        // Scroll output into view on small screens
        outputContent.scrollIntoView({ behavior: 'smooth' });
    }

    // 5. Action Handlers (Copy & Print)
    copyMdBtn.addEventListener('click', () => {
        if (!compiledMarkdown) return;
        navigator.clipboard.writeText(compiledMarkdown)
            .then(() => {
                const originalText = copyMdBtn.innerText;
                copyMdBtn.innerText = "Copied!";
                setTimeout(() => { copyMdBtn.innerText = originalText; }, 2000);
            })
            .catch(err => {
                console.error("Clipboard copy failed:", err);
                alert("Failed to copy markdown. Please select and copy manually.");
            });
    });

    printBtn.addEventListener('click', () => {
        window.print();
    });
});
