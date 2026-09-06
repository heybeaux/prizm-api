public without sharing class WCPrizmDailyEnrichment implements Schedulable, Database.AllowsCallouts {

    private static final Set<String> INCOME_VALUES = new Set<String>{
        '$0 to $110,593',
        '$110,594 to $142,817',
        '$142,818 to $226,869',
        '$226,870+'
    };

    private static final Set<String> NET_WORTH_VALUES = new Set<String>{
        '$0 to $600K',
        '$600K to $1M',
        '$1M to $2.15M',
        '$2.15M+'
    };

    private static final Set<String> HOME_TYPE_VALUES = new Set<String>{
        'None or Unverifiable',
        'Condo/Strata',
        'Average Median Detached Home',
        'Above Average Median Detached Home',
        'Apartments',
        'Apt 5+',
        'Low Rise Apt / Duplex / Semi',
        'Mixed',
        'Row / Low Rise Apt',
        'Single Detached',
        'Single Detached / Duplex / Row',
        'Single Detached / Low Rise Apt',
        'Single Detached / Moveable',
        'Single Detached / Semi / Row',
        'Duplex / Single Detached / Low Rise Apt',
        'Low Rise Apt',
        'Duplex / Low Rise Apt / Single Detached'
    };

    @InvocableMethod(
        label='Run PRIZM Daily Enrichment'
        description='Captures up to 10 new Canadian major-donor postal codes daily and updates matching Household Accounts.'
    )
    public static void runFromFlow(List<String> ignoredInputs) {
        runQueued();
    }

    public void execute(SchedulableContext context) {
        runQueued();
    }

    public static Id runQueued() {
        return System.enqueueJob(new PrizmDailyQueueable(10));
    }

    public static Boolean runDaily() {
        // Railway owns cohort membership and permanent capture history.
        // Never derive new capture eligibility from optional Salesforce fields.
        Map<String, PrizmResult> resultsByPostalCode = fetchPrizmResults();
        Map<String, Set<String>> rawCodesByNormalized = new Map<String, Set<String>>();
        for (String code : resultsByPostalCode.keySet()) {
            rawCodesByNormalized.put(code, new Set<String>{code, code.replace(' ', '')});
        }
        if (resultsByPostalCode.isEmpty()) {
            return false;
        }

        Set<String> rawCodesToUpdate = new Set<String>();
        for (String normalizedPostalCode : resultsByPostalCode.keySet()) {
            if (rawCodesByNormalized.containsKey(normalizedPostalCode)) {
                rawCodesToUpdate.addAll(rawCodesByNormalized.get(normalizedPostalCode));
            }
        }

        if (rawCodesToUpdate.isEmpty()) {
            return false;
        }

        List<Account> updates = new List<Account>();
        for (Account account : [
            SELECT Id, BillingPostalCode
            FROM Account
            WHERE RecordType.DeveloperName = 'HH_Account'
            AND BillingCountryCode = 'CA'
            AND BillingPostalCode IN :rawCodesToUpdate
            AND (PRIZM_Score__c = null OR PRIZM_Score__c != 0)
            AND (
                PRIZM_Score__c = null
                OR Average_Income_in_Postal_Code__c = null
                OR Property_ownership_and_value__c = null
            )
            LIMIT 9000
        ]) {
            String normalizedPostalCode = normalizePostalCode(account.BillingPostalCode);
            PrizmResult result = resultsByPostalCode.get(normalizedPostalCode);
            if (result == null) {
                continue;
            }

            Account updateAccount = new Account(Id = account.Id);
            if (result.status == 'success') {
                updateAccount.PRIZM_Score__c = parseDecimal(result.segmentNumber);
                updateAccount.Average_Income_in_Postal_Code__c = mapIncome(result.averageHouseholdIncome);
                // Net worth is optional historical reference data, not a completion requirement.
                String netWorth = mapNetWorth(result.averageHouseholdNetWorth, result.averageHouseholdNetWorthAmount);
                if (netWorth != null) {
                    updateAccount.Average_Net_Worth_in_Postal_Code__c = netWorth;
                }
                updateAccount.WC_Neighbourhood_description__c = firstNonBlank(new List<String>{ result.whoTheyAre, result.segmentDescription });
                updateAccount.Property_ownership_and_value__c = mapHomeType(result.homeType);
            } else if (result.retryable) {
                continue;
            } else {
                updateAccount.PRIZM_Score__c = 0;
                updateAccount.WC_Neighbourhood_description__c = firstNonBlank(new List<String>{
                    result.message,
                    'PRIZM API did not assign this postal code to a segment.'
                });
                updateAccount.Property_ownership_and_value__c = 'None or Unverifiable';
            }
            updates.add(updateAccount);
        }

        if (!updates.isEmpty()) {
            // Fail the job visibly and roll back the batch if any household update fails.
            // Ignoring SaveResult previously made unsuccessful updates look completed.
            update updates;
        }
        return true;
    }

    @TestVisible
    private static Map<String, PrizmResult> fetchPrizmResults() {
        HttpRequest request = new HttpRequest();
        request.setMethod('POST');
        request.setEndpoint(System.Label.WC_PRIZM_API_Base_URL + '/api/cohort/capture');
        request.setHeader('Content-Type', 'application/json');
        request.setHeader('X-API-Key', System.Label.WC_PRIZM_API_Key);
        request.setTimeout(120000);
        request.setBody('{}');

        HttpResponse response = new Http().send(request);
        if (response.getStatusCode() < 200 || response.getStatusCode() >= 300) {
            throw new CalloutException('PRIZM API returned HTTP ' + response.getStatusCode() + ': ' + response.getBody());
        }

        Map<String, Object> payload = (Map<String, Object>) JSON.deserializeUntyped(response.getBody());
        List<Object> resultRows = (List<Object>) payload.get('results');
        Map<String, PrizmResult> results = new Map<String, PrizmResult>();
        if (resultRows == null) {
            return results;
        }

        for (Object item : resultRows) {
            Map<String, Object> row = (Map<String, Object>) item;
            PrizmResult result = new PrizmResult(row);
            String normalizedPostalCode = normalizePostalCode(result.postalCode);
            if (!String.isBlank(normalizedPostalCode)) {
                results.put(normalizedPostalCode, result);
            }
        }
        return results;
    }

    @TestVisible
    private static String normalizePostalCode(String postalCode) {
        if (String.isBlank(postalCode)) {
            return null;
        }

        String compact = postalCode.replaceAll('\\s', '').toUpperCase();
        if (!Pattern.matches('^[A-Z][0-9][A-Z][0-9][A-Z][0-9]$', compact)) {
            return null;
        }
        return compact.substring(0, 3) + ' ' + compact.substring(3);
    }

    private static Decimal parseDecimal(String value) {
        if (String.isBlank(value)) {
            return null;
        }
        try {
            return Decimal.valueOf(value);
        } catch (Exception ignored) {
            return null;
        }
    }

    private static String mapIncome(String value) {
        if (String.isBlank(value)) {
            return null;
        }
        if (INCOME_VALUES.contains(value)) {
            return value;
        }

        Decimal amount = parseCurrency(value);
        if (amount == null) {
            return null;
        }
        if (amount <= 110593) {
            return '$0 to $110,593';
        }
        if (amount <= 142817) {
            return '$110,594 to $142,817';
        }
        if (amount <= 226869) {
            return '$142,818 to $226,869';
        }
        return '$226,870+';
    }

    private static String mapNetWorth(String value, Decimal amount) {
        if (!String.isBlank(value) && NET_WORTH_VALUES.contains(value)) {
            return value;
        }
        if (amount == null) {
            amount = parseCurrency(value);
        }
        if (amount == null) {
            return null;
        }
        if (amount <= 600000) {
            return '$0 to $600K';
        }
        if (amount <= 1000000) {
            return '$600K to $1M';
        }
        if (amount <= 2150000) {
            return '$1M to $2.15M';
        }
        return '$2.15M+';
    }

    private static Decimal parseCurrency(String value) {
        if (String.isBlank(value)) {
            return null;
        }
        String cleaned = value.replace('$', '').replace(',', '').trim();
        if (!Pattern.matches('^[0-9]+(\\.[0-9]+)?$', cleaned)) {
            return null;
        }
        return Decimal.valueOf(cleaned);
    }

    private static String mapHomeType(String value) {
        if (String.isBlank(value)) {
            return null;
        }

        String normalizedInput = normalizePicklistComparison(value);
        for (String candidate : HOME_TYPE_VALUES) {
            if (normalizePicklistComparison(candidate) == normalizedInput) {
                return candidate;
            }
        }
        return 'None or Unverifiable';
    }

    private static String normalizePicklistComparison(String value) {
        return value == null ? '' : value.toLowerCase().replaceAll('[^a-z0-9]', '');
    }

    private static String firstNonBlank(List<String> values) {
        for (String value : values) {
            if (!String.isBlank(value)) {
                return value;
            }
        }
        return null;
    }

    private class PrizmDailyQueueable implements Queueable, Database.AllowsCallouts {
        private Integer remaining;
        private PrizmDailyQueueable(Integer remaining) {
            this.remaining = remaining;
        }
        public void execute(QueueableContext context) {
            if (runDaily() && remaining > 1) {
                System.enqueueJob(new PrizmDailyQueueable(remaining - 1));
            }
        }
    }

    private class PrizmResult {
        private String postalCode;
        private String segmentNumber;
        private String segmentDescription;
        private String whoTheyAre;
        private String averageHouseholdIncome;
        private String averageHouseholdNetWorth;
        private Decimal averageHouseholdNetWorthAmount;
        private String homeType;
        private String status;
        private String message;
        private Boolean retryable;

        private PrizmResult(Map<String, Object> row) {
            postalCode = (String) row.get('postal_code');
            segmentNumber = stringValue(row.get('segment_number'));
            segmentDescription = (String) row.get('segment_description');
            whoTheyAre = (String) row.get('who_they_are');
            averageHouseholdIncome = (String) row.get('average_household_income');
            averageHouseholdNetWorth = (String) row.get('average_household_net_worth');
            averageHouseholdNetWorthAmount = decimalValue(row.get('average_household_net_worth_amount'));
            homeType = (String) row.get('home_type');
            status = (String) row.get('status');
            retryable = row.get('retryable') == true;
            message = (String) row.get('message');
        }

        private String stringValue(Object value) {
            return value == null ? null : String.valueOf(value);
        }

        private Decimal decimalValue(Object value) {
            if (value == null) {
                return null;
            }
            try {
                return Decimal.valueOf(String.valueOf(value));
            } catch (Exception ignored) {
                return null;
            }
        }
    }
}