{
    "name": "Supplier Analytics",
    "version": "1.0",
    "depends": ["purchase", "stock"],
    "data": [
        "security/ir.model.access.csv",
        "views/res_partner_view.xml",
        "views/monthly_spend_views.xml",
        "data/cron_monthly_spend.xml",
    ],
    "installable": True,
    "application": True,
}
