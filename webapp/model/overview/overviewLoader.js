sap.ui.define([
    "disertatie/model/api/apiClient",
    "disertatie/model/presentation/basicFormat",
    "disertatie/model/presentation/overviewPresenter"
], (ApiClient, BasicFormat, OverviewPresenter) => {
    "use strict";

    const { fetchJson, getApiUrl } = ApiClient;
    const { formatLocalDate } = BasicFormat;
    const { overviewUnavailable, prepareOverview } = OverviewPresenter;

    function loadHello(model) {
        return fetch(getApiUrl("/api/hello").toString())
            .then((response) => response.json())
            .then((result) => {
                if (result && result.message) {
                    model.setProperty("/helloMessage", result.message);
                } else {
                    model.setProperty("/helloMessage", "Irrigation service connected but returned unexpected response");
                }
            })
            .catch(() => {
                model.setProperty("/helloMessage", "Irrigation service connection failed");
            });
    }

    function loadOverview(model, afterLoad) {
        const url = getApiUrl("/api/overview");
        url.searchParams.set("_", String(Date.now()));
        return fetchJson(url.toString(), { cache: "no-store" })
            .then((result) => {
                model.setProperty("/overview", prepareOverview(result));
                if (typeof afterLoad === "function") {
                    afterLoad(result);
                }
            })
            .catch((error) => {
                model.setProperty("/overview", overviewUnavailable(error));
            });
    }

    function loadWeatherAvailability(model) {
        return fetch(getApiUrl("/api/weather/cluj-napoca/summary").toString())
            .then((response) => response.json())
            .then((result) => {
                const rows = Array.isArray(result.hourly_weather) ? result.hourly_weather : [];
                const maxDate = rows.reduce((currentMax, row) => {
                    const timestamp = row.last_timestamp ? new Date(row.last_timestamp) : null;
                    if (!timestamp || Number.isNaN(timestamp.getTime())) {
                        return currentMax;
                    }
                    return !currentMax || timestamp > currentMax ? timestamp : currentMax;
                }, null);
                if (maxDate) {
                    model.setProperty("/weatherAvailability/maxWeatherDate", formatLocalDate(maxDate));
                }
            })
            .catch(() => {
                model.setProperty("/weatherAvailability/maxWeatherDate", null);
            });
    }

    return {
        loadHello,
        loadOverview,
        loadWeatherAvailability
    };
});
