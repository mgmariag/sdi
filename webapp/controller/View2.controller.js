sap.ui.define([
    "sap/ui/core/mvc/Controller",
    "sap/ui/model/json/JSONModel",
    "disertatie.model.digitalTwinSampleData"
], function (Controller, JSONModel, sampleData) {
    "use strict";

    return Controller.extend("disertatie.controller.View2", {
        onInit() {
            const entries = sampleData.createData(100);
            const data = {
                dtMessage: "Digital Twin Sample Data",
                dtData: entries
            };

            const oModel = new JSONModel(data);
            this.getView().setModel(oModel);
        }
    });
});
