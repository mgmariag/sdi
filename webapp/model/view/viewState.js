/*eslint camelcase: "off"*/
sap.ui.define([
    "disertatie/model/presentation/overviewPresenter",
    "disertatie/model/experiment/experimentDefaults"
], (OverviewPresenter, ExperimentDefaults) => {
    "use strict";

    const { defaultOverview } = OverviewPresenter;
    const {
        RECOMMENDED_SAMPLING_INTERVAL_HOURS,
        defaultAnfisSummary,
        defaultExperimentFooter,
        defaultExperimentRange,
        defaultFuzzySummary,
        defaultSamplingSummary
    } = ExperimentDefaults;

    function initialData() {
        const defaultRange = defaultExperimentRange();

        return {
            helloMessage: "Welcome! Attempting to connect to irrigation service...",
            experimentSettings: {
                start_date: defaultRange.start,
                end_date: defaultRange.end
            },
            samplingSettings: {
                sample_interval_hours: RECOMMENDED_SAMPLING_INTERVAL_HOURS
            },
            sensorSettings: {
                sensor_count: null
            },
            rangeAlert: {
                visible: false,
                text: ""
            },
            overview: defaultOverview(),
            weatherAvailability: {
                maxWeatherDate: null
            },
            chartVisibility: {
                baseline: true,
                weather: true
            },
            experimentFooter: defaultExperimentFooter(),
            sensorPlacements: [],
            sensorPlacementSummary: {
                sensor_count: null,
                minimum_sensor_count: 5,
                valve_count: 5,
                has_all_valve_zones: false,
                stored_sensor_count: 0,
                sensor_reading_pot_count: 0,
                active_pot_count: 0,
                updated_at: null,
                loaded: false
            },
            samplingEntries: [],
            samplingChartAllEntries: [],
            samplingChartEntries: [],
            samplingPots: [],
            anfisEntries: [],
            anfisChartAllEntries: [],
            anfisChartEntries: [],
            anfisPots: [],
            fuzzyEntries: [],
            fuzzyChartAllEntries: [],
            fuzzyChartEntries: [],
            fuzzyPots: [],
            samplingSummary: defaultSamplingSummary(RECOMMENDED_SAMPLING_INTERVAL_HOURS),
            anfisSummary: defaultAnfisSummary(),
            fuzzySummary: defaultFuzzySummary(),
            activeExperiment: null,
            isSensorPlacementLoading: false,
            isSamplingLoading: false,
            isAnfisLoading: false,
            isFuzzyLoading: false
        };
    }

    return {
        initialData
    };
});
