sap.ui.define([
    "sap/ui/core/UIComponent",
    "disertatie/model/models"
], (UIComponent, models) => {
    "use strict";

    return UIComponent.extend("disertatie.Component", {
        metadata: {
            manifest: "json",
            interfaces: [
                "sap.ui.core.IAsyncContentCreation"
            ]
        },

        init() {
            // call the base component's init function
            UIComponent.prototype.init.apply(this, arguments);

            // show startup alert
            alert("Welcome to the app!");

            // set the device model
            this.setModel(models.createDeviceModel(), "device");

            // enable routing
            const router = this.getRouter();
            if (router) {
                router.initialize();
            }
        }
    });
});