sap.ui.define([
    "sap/ui/core/UIComponent",
    "disertatie/model/flpUser",
    "disertatie/model/models"
], (UIComponent, flpUser, models) => {
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

            // set the device model
            this.setModel(models.createDeviceModel(), "device");
            flpUser.install();

            // enable routing
            const router = this.getRouter();
            if (router) {
                router.initialize();
            }
        }
    });
});
