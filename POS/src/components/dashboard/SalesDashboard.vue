<template>
	<Dialog v-model="show" :options="{ size: '4xl' }">
		<template #body>
			<div class="p-6">
				<div class="flex items-center justify-between mb-4">
					<h2 class="text-lg font-semibold">{{ __("Sales Dashboard") }}</h2>
					<button @click="show = false" class="text-gray-400 hover:text-gray-600">
						<FeatherIcon name="x" class="w-5 h-5" />
					</button>
				</div>

				<div v-if="loading" class="text-center py-12 text-gray-400">
					{{ __("Loading...") }}
				</div>

				<div v-else class="space-y-6">
					<!-- Summary cards -->
					<div class="grid grid-cols-4 gap-4">
						<div class="border rounded-lg p-4">
							<div class="text-xs text-gray-500">{{ __("Today's Sales") }}</div>
							<div class="text-xl font-semibold">{{ formatCurrency(data.daily?.total_sales) }}</div>
						</div>
						<div class="border rounded-lg p-4">
							<div class="text-xs text-gray-500">{{ __("Today's Invoices") }}</div>
							<div class="text-xl font-semibold">{{ data.daily?.invoice_count || 0 }}</div>
						</div>
						<div class="border rounded-lg p-4">
							<div class="text-xs text-gray-500">{{ __("Avg Ticket") }}</div>
							<div class="text-xl font-semibold">{{ formatCurrency(data.daily?.avg_ticket) }}</div>
						</div>
						<div class="border rounded-lg p-4">
							<div class="text-xs text-gray-500">{{ __("This Month") }}</div>
							<div class="text-xl font-semibold">{{ formatCurrency(data.month?.total_sales) }}</div>
						</div>
					</div>

					<!-- Group-wise -->
					<div>
						<h3 class="text-sm font-semibold mb-2">{{ __("Sales by Item Group") }}</h3>
						<table class="w-full text-sm">
							<thead>
								<tr class="text-left text-gray-500 border-b">
									<th class="py-1">{{ __("Group") }}</th>
									<th class="py-1 text-right">{{ __("Qty") }}</th>
									<th class="py-1 text-right">{{ __("Sales") }}</th>
								</tr>
							</thead>
							<tbody>
								<tr v-for="row in data.group_wise" :key="row.item_group" class="border-b">
									<td class="py-1">{{ row.item_group }}</td>
									<td class="py-1 text-right">{{ row.qty_sold }}</td>
									<td class="py-1 text-right">{{ formatCurrency(row.total_sales) }}</td>
								</tr>
							</tbody>
						</table>
					</div>

					<!-- Item-wise (top sellers) -->
					<div>
						<h3 class="text-sm font-semibold mb-2">{{ __("Top Selling Items (Today)") }}</h3>
						<table class="w-full text-sm">
							<thead>
								<tr class="text-left text-gray-500 border-b">
									<th class="py-1">{{ __("Item") }}</th>
									<th class="py-1 text-right">{{ __("Qty") }}</th>
									<th class="py-1 text-right">{{ __("Sales") }}</th>
								</tr>
							</thead>
							<tbody>
								<tr v-for="row in data.item_wise" :key="row.item_code" class="border-b">
									<td class="py-1">{{ row.item_name }}</td>
									<td class="py-1 text-right">{{ row.qty_sold }}</td>
									<td class="py-1 text-right">{{ formatCurrency(row.total_sales) }}</td>
								</tr>
							</tbody>
						</table>
					</div>

					<!-- Payment modes -->
					<div>
						<h3 class="text-sm font-semibold mb-2">{{ __("Payment Modes (Today)") }}</h3>
						<div class="flex gap-4">
							<div v-for="row in data.payment_modes" :key="row.mode_of_payment" class="border rounded-lg p-3 flex-1">
								<div class="text-xs text-gray-500">{{ row.mode_of_payment }}</div>
								<div class="font-semibold">{{ formatCurrency(row.total_amount) }}</div>
							</div>
						</div>
					</div>
				</div>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
import { ref, watch, onUnmounted } from "vue";
import { Dialog, FeatherIcon } from "frappe-ui";
import { call } from "@/utils/apiWrapper";
import { formatCurrency as formatCurrencyUtil } from "@/utils/currency";

const props = defineProps({
	modelValue: Boolean,
	posProfile: String,
	currency: String,
});
const emit = defineEmits(["update:modelValue"]);

const show = ref(props.modelValue);
const loading = ref(false);
const data = ref({});
let refreshTimer = null;

watch(() => props.modelValue, (val) => {
	show.value = val;
	if (val) {
		loadData();
		refreshTimer = setInterval(loadData, 30000); // live refresh every 30s
	} else {
		clearInterval(refreshTimer);
	}
});
watch(show, (val) => emit("update:modelValue", val));

async function loadData() {
	loading.value = data.value.daily === undefined;
	try {
		const res = await call("pos_next.api.dashboard.get_dashboard_summary", {
			pos_profile: props.posProfile,
		});
		data.value = res;
	} finally {
		loading.value = false;
	}
}

function formatCurrency(amount) {
	return formatCurrencyUtil(Number.parseFloat(amount || 0), props.currency);
}

onUnmounted(() => clearInterval(refreshTimer));
</script>