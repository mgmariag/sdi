sap.ui.define([
    "sap/ui/core/mvc/Controller",
     "sap/ui/model/json/JSONModel"
], (Controller, JSONModel) => {
    "use strict";

    return Controller.extend("disertatie.controller.View1", {
        onInit() {
            const initialData = {
                helloMessage: "Loading hello message...",
                experimentSettings: {
                    steps: 1000,
                    threshold: 35,
                    hysteresis: 4,
                    flow_rate: 1.0
                },
                processedEntries: [],
                summary: {
                    totalEntries: 0,
                    irrigationEvents: 0,
                    irrigationSteps: 0,
                    totalWaterUsage: 0,
                    percentTimeIrrigated: 0
                },
                isLoading: false
            };

            const oModel = new JSONModel(initialData);
            this.getView().setModel(oModel);

            const sUrl = "http://127.0.0.1:8000/api/hello";
            fetch(sUrl)
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.message) {
                        oModel.setProperty("/helloMessage", result.message);
                    } else {
                        oModel.setProperty("/helloMessage", "Hello message not available");
                    }
                })
                .catch(() => {
                    oModel.setProperty("/helloMessage", "Unable to load hello message");
                });

            // Run initial experiment
            this.onRunExperiment();
        },

        onRunExperiment() {
            const model = this.getView().getModel();
            const settings = model.getProperty("/experimentSettings") || {};
            
            model.setProperty("/isLoading", true);

            const url = new URL("http://127.0.0.1:8000/api/experiment");
            url.searchParams.append("steps", settings.steps || 1000);
            url.searchParams.append("threshold", settings.threshold || 35);
            url.searchParams.append("hysteresis", settings.hysteresis || 0);
            url.searchParams.append("flow_rate", settings.flow_rate || 1.0);

            fetch(url.toString())
                .then((response) => response.json())
                .then((result) => {
                    if (result && result.entries && result.summary) {
                        model.setProperty("/processedEntries", result.entries);
                        model.setProperty("/summary", result.summary);
                    }
                    model.setProperty("/isLoading", false);
                })
                .catch((error) => {
                    console.error("Experiment error:", error);
                    model.setProperty("/isLoading", false);
                });
        }
    });
});
