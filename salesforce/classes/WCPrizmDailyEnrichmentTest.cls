@IsTest
private class WCPrizmDailyEnrichmentTest {
    private class PrizmMock implements HttpCalloutMock {
        public HTTPResponse respond(HTTPRequest request) {
            System.assertEquals('POST', request.getMethod());
            System.assert(request.getEndpoint().endsWith('/api/cohort/capture'));

            HttpResponse response = new HttpResponse();
            response.setStatusCode(200);
            response.setHeader('Content-Type', 'application/json');
            response.setBody('{"results":[{"postal_code":"V8A 2P4","segment_number":"62","segment_description":"Older suburban singles","who_they_are":"Suburban Recliners description","average_household_income":"$94,148","average_household_net_worth":"$0 to $600K","average_household_net_worth_amount":461727,"home_type":"Single Detached/Low Rise Apt","status":"success"},{"postal_code":"V5U 1J4","prizm_code":"Unknown","segment_number":null,"message":"Postal code was not assigned to a PRIZM segment","status":"error"}],"total":2,"successful":1,"failed":1}');
            return response;
        }
    }

    @IsTest
    static void runDailyUpdatesMatchingHouseholdAccounts() {
        Id householdRecordTypeId = [
            SELECT Id
            FROM RecordType
            WHERE SobjectType = 'Account'
            AND DeveloperName = 'HH_Account'
            LIMIT 1
        ].Id;

        Account accountOne = new Account(
            Name = 'PRIZM Test One',
            RecordTypeId = householdRecordTypeId,
            BillingStreet = '1 Test St',
            BillingCity = 'Powell River',
            BillingCountryCode = 'CA',
            BillingStateCode = 'BC',
            BillingPostalCode = 'V8A2P4'
        );
        Account accountTwo = new Account(
            Name = 'PRIZM Test Two',
            RecordTypeId = householdRecordTypeId,
            BillingStreet = '2 Test St',
            BillingCity = 'Powell River',
            BillingCountryCode = 'CA',
            BillingStateCode = 'BC',
            BillingPostalCode = 'V8A 2P4'
        );
        Account noSegmentAccount = new Account(
            Name = 'PRIZM No Segment',
            RecordTypeId = householdRecordTypeId,
            BillingStreet = '3 Test St',
            BillingCity = 'Vancouver',
            BillingCountryCode = 'CA',
            BillingStateCode = 'BC',
            BillingPostalCode = 'V5U 1J4'
        );
        insert new List<Account>{ accountOne, accountTwo, noSegmentAccount };

        Test.setMock(HttpCalloutMock.class, new PrizmMock());

        Test.startTest();
        WCPrizmDailyEnrichment.runDaily();
        Test.stopTest();

        for (Account updatedAccount : [
            SELECT PRIZM_Score__c,
                   Average_Income_in_Postal_Code__c,
                   Average_Net_Worth_in_Postal_Code__c,
                   WC_Neighbourhood_description__c,
                   Property_ownership_and_value__c
            FROM Account
            WHERE Id IN :new Set<Id>{ accountOne.Id, accountTwo.Id }
        ]) {
            System.assertEquals(62, updatedAccount.PRIZM_Score__c);
            System.assertEquals('$0 to $110,593', updatedAccount.Average_Income_in_Postal_Code__c);
            System.assertEquals('$0 to $600K', updatedAccount.Average_Net_Worth_in_Postal_Code__c);
            System.assertEquals('Suburban Recliners description', updatedAccount.WC_Neighbourhood_description__c);
            System.assertEquals('Single Detached / Low Rise Apt', updatedAccount.Property_ownership_and_value__c);
        }

        Account noSegmentUpdated = [
            SELECT PRIZM_Score__c,
                   Average_Income_in_Postal_Code__c,
                   Average_Net_Worth_in_Postal_Code__c,
                   WC_Neighbourhood_description__c,
                   Property_ownership_and_value__c
            FROM Account
            WHERE Id = :noSegmentAccount.Id
        ];
        System.assertEquals(0, noSegmentUpdated.PRIZM_Score__c);
        System.assertEquals(null, noSegmentUpdated.Average_Income_in_Postal_Code__c);
        System.assertEquals(null, noSegmentUpdated.Average_Net_Worth_in_Postal_Code__c);
        System.assertEquals('Postal code was not assigned to a PRIZM segment', noSegmentUpdated.WC_Neighbourhood_description__c);
        System.assertEquals('None or Unverifiable', noSegmentUpdated.Property_ownership_and_value__c);
    }

    @IsTest
    static void schedulableEntryPointRuns() {
        Test.setMock(HttpCalloutMock.class, new EmptyCohortMock());

        Test.startTest();
        System.schedule('WC PRIZM Test Job', '0 0 2 * * ?', new WCPrizmDailyEnrichment());
        Test.stopTest();

        System.assertEquals('V8A 2P4', WCPrizmDailyEnrichment.normalizePostalCode('v8a2p4'));
    }

    private class EmptyCohortMock implements HttpCalloutMock {
        public HTTPResponse respond(HTTPRequest request) {
            System.assert(request.getEndpoint().endsWith('/api/cohort/capture'));
            System.assertEquals('{}', request.getBody());
            HttpResponse response = new HttpResponse();
            response.setStatusCode(200);
            response.setBody('{"results":[],"summary":{"pending":0}}');
            return response;
        }
    }

    @IsTest
    static void exhaustedCohortDoesNotFallBackToSalesforce() {
        Test.setMock(HttpCalloutMock.class, new EmptyCohortMock());
        Test.startTest();
        WCPrizmDailyEnrichment.runDaily();
        System.assertEquals(1, Limits.getCallouts());
        System.assertEquals(0, Limits.getDmlStatements());
        Test.stopTest();
    }

    private class MissingNetWorthMock implements HttpCalloutMock {
        public HTTPResponse respond(HTTPRequest request) {
            HttpResponse response = new HttpResponse();
            response.setStatusCode(200);
            response.setBody('{"results":[{"postal_code":"V8A 2P4","segment_number":"66","average_household_income":"$98,721","home_type":"Single Detached","status":"success"},{"postal_code":"V5U 1J4","status":"error","retryable":true,"message":"quota unavailable"}]}');
            return response;
        }
    }

    @IsTest
    static void preservesKnownNetWorthAndRetriesTransientFailures() {
        Id householdType = [SELECT Id FROM RecordType WHERE SobjectType = 'Account' AND DeveloperName = 'HH_Account' LIMIT 1].Id;
        Account known = new Account(
            Name = 'Preserve historical value', RecordTypeId = householdType,
            BillingStreet = '1 Test St', BillingCity = 'Powell River', BillingCountryCode = 'CA',
            BillingStateCode = 'BC', BillingPostalCode = 'V8A 2P4',
            Average_Net_Worth_in_Postal_Code__c = '$600K to $1M'
        );
        Account retry = new Account(
            Name = 'Retry quota failure', RecordTypeId = householdType,
            BillingStreet = '2 Test St', BillingCity = 'Vancouver', BillingCountryCode = 'CA',
            BillingStateCode = 'BC', BillingPostalCode = 'V5U 1J4'
        );
        insert new List<Account>{ known, retry };
        Test.setMock(HttpCalloutMock.class, new MissingNetWorthMock());
        Test.startTest();
        System.assertEquals(false, WCPrizmDailyEnrichment.runDaily(), 'Stop chaining when quota or network availability fails.');
        Test.stopTest();
        known = [SELECT Average_Net_Worth_in_Postal_Code__c FROM Account WHERE Id = :known.Id];
        retry = [SELECT PRIZM_Score__c FROM Account WHERE Id = :retry.Id];
        System.assertEquals('$600K to $1M', known.Average_Net_Worth_in_Postal_Code__c);
        System.assertEquals(null, retry.PRIZM_Score__c, 'Temporary quota failures must not permanently exclude households.');
    }
}