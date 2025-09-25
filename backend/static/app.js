// backend/static/app.js
async function apiFetch(path, opts = {}) {
    const token = localStorage.getItem('jwt');
    opts.headers = opts.headers || {};
    if (token) opts.headers['Authorization'] = 'Bearer ' + token;
    const res = await fetch(path, opts);
    return res.json();
}

function renderCard(item, idx) {
    const div = document.createElement('div');
    div.className = 'card';
    const title = document.createElement('h3');
    title.textContent = item.card_name;
    const img = document.createElement('img');
    img.src = item.last_image || '/static/no_image.png';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.innerHTML = `Updated: ${item.last_updated || 'N/A'} <br> Confirmed: ${item.confirmed ? 'Yes' : 'No'}`;
    const btnRefresh = document.createElement('button');
    btnRefresh.textContent = 'Refresh';
    btnRefresh.onclick = async () => {
        await apiFetch('/api/refresh', { method: 'POST', body: new URLSearchParams({ card_name: item.card_name }) });
        loadSaved();
    };
    const btnConfirm = document.createElement('button');
    btnConfirm.textContent = 'Confirm Image';
    btnConfirm.onclick = async () => {
        await apiFetch('/api/confirm_image', { method: 'POST', body: new URLSearchParams({ card_name: item.card_name, image_url: img.src }) });
        loadSaved();
    };
    // Chart canvas
    const canvas = document.createElement('canvas');
    canvas.id = 'chart_' + idx;
    div.appendChild(title);
    div.appendChild(img);
    div.appendChild(meta);
    div.appendChild(btnRefresh);
    div.appendChild(btnConfirm);
    div.appendChild(canvas);

    // render chart if data
    if (item.last_result && item.last_result.avg) {
        const labels = Object.keys(item.last_result.avg);
        const data = Object.values(item.last_result.avg);
        new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: { labels, datasets: [{ label: 'Avg (local)', data }] },
            options: { responsive: true, scales: { y: { beginAtZero: true } } }
        });
    }
    return div;
}

async function loadSaved() {
    const grid = document.getElementById('resultsGrid');
    grid.innerHTML = '';
    const data = await apiFetch('/api/saved');
    if (data && Array.isArray(data)) {
        data.forEach((d, i) => grid.appendChild(renderCard(d, i)));
    } else {
        grid.innerHTML = '<p>No saved searches (login & search to save).</p>';
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const searchForm = document.getElementById('searchForm');
    if (searchForm) {
        searchForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const name = document.getElementById('cardInput').value.trim();
            const region = document.getElementById('regionSelect').value;
            const body = new URLSearchParams({ card_name: name, region });
            const result = await apiFetch('/api/search', { method: 'POST', body });
            if (result && result.ok) {
                loadSaved();
            } else {
                alert('Search failed. Ensure you are logged in and have a valid EBAY_OAUTH_TOKEN on the server.');
            }
        });
    }
    loadSaved();
});
