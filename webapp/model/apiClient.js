sap.ui.define([], () => {
    "use strict";

    function getApiUrl(path) {
        if (window.__DT_API_BASE_URL) {
            return new URL(path, window.__DT_API_BASE_URL);
        }

        if (window.location.port !== "8080" && window.location.port !== "8081") {
            return new URL(path, window.location.origin);
        }

        return new URL(path, `${window.location.protocol}//${window.location.hostname}:8000`);
    }

    function fetchJson(url, options) {
        return fetch(url, options).then((response) => {
            return response.json()
                .catch(() => ({}))
                .then((body) => {
                    if (!response.ok) {
                        const error = new Error(body && body.detail && body.detail.message ? body.detail.message : response.statusText);
                        error.status = response.status;
                        error.detail = body && body.detail;
                        throw error;
                    }
                    return body;
                });
        });
    }

    return {
        fetchJson,
        getApiUrl
    };
});
