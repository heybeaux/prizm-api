public without sharing class WCPrizmWeeklyReportEmail implements Schedulable {
    private static final String DEFAULT_RECIPIENT = 'dena@wildernesscommittee.org';

    public void execute(SchedulableContext context) {
        runQueued();
    }

    public static Id runQueued() {
        return System.enqueueJob(new WeeklyReportQueueable());
    }

    private class WeeklyReportQueueable implements Queueable, Database.AllowsCallouts {
        public void execute(QueueableContext context) {
            HttpRequest request = new HttpRequest();
            request.setMethod('GET');
            request.setEndpoint(System.Label.WC_PRIZM_API_Base_URL + '/api/reports/weekly');
            request.setHeader('X-API-Key', System.Label.WC_PRIZM_API_Key);
            request.setTimeout(60000);

            HttpResponse response = new Http().send(request);
            if (response.getStatusCode() < 200 || response.getStatusCode() >= 300) {
                throw new CalloutException('PRIZM weekly report returned HTTP ' + response.getStatusCode() + ': ' + response.getBody());
            }

            Map<String, Object> payload = (Map<String, Object>) JSON.deserializeUntyped(response.getBody());
            String subject = stringValue(payload.get('subject'));
            String body = stringValue(payload.get('body'));

            if (String.isBlank(subject)) {
                subject = 'PRIZM weekly report';
            }
            if (String.isBlank(body)) {
                body = 'The PRIZM weekly report endpoint returned no report body.';
            }

            Messaging.SingleEmailMessage email = new Messaging.SingleEmailMessage();
            email.setToAddresses(new List<String>{ DEFAULT_RECIPIENT });
            email.setSubject(subject);
            email.setPlainTextBody(body);
            Messaging.SendEmailResult[] sent = Messaging.sendEmail(new List<Messaging.SingleEmailMessage>{ email });
            if (!sent[0].isSuccess()) {
                throw new CalloutException('PRIZM report email failed: ' + sent[0].getErrors()[0].getMessage());
            }
        }

        private String stringValue(Object value) {
            return value == null ? null : String.valueOf(value);
        }
    }
}
