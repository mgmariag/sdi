sap.ui.define([], function () {
    "use strict";

    function generateEntry(baseTimeMs, index) {
        const ts = new Date(baseTimeMs + index * 60000).toISOString(); // 1 minute steps

        // simple synthetic signals
        const moisture = Math.round(50 + 15 * Math.sin(index / 6));
        const temperature = Math.round(20 + 6 * Math.sin(index / 12 + 1));
        const humidity = Math.round(60 + 20 * Math.sin(index / 8 + 2));
        const irrigationActive = moisture < 30;

        return {
            timestamp: ts,
            moisture: moisture,
            temperature: temperature,
            humidity: humidity,
            irrigation_active: irrigationActive
        };
    }

    return {
        /**
         * Create N synthetic digital twin entries.
         * @param {number} count number of entries to create (default 100)
         * @returns {Array<Object>} array of sample entries
         */
        createData: function (count) {
            count = count || 100;
            const now = new Date();
            const baseTimeMs = now.getTime();
            const out = [];
            for (let i = 0; i < count; i++) {
                out.push(generateEntry(baseTimeMs, i));
            }
            return out;
        }
    };
});
